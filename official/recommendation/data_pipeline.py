# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Run specific portions of the NCF data pipeline."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import multiprocessing.dummy
import os
import pickle
import struct
import threading
import time
import timeit

import numpy as np
from six.moves import queue
import tensorflow as tf

from official.datasets import movielens
from official.recommendation import constants as rconst
from official.recommendation import stat_utils


SUMMARY_TEMPLATE = """General:
{spacer}Num users: {num_users}
{spacer}Num items: {num_items}

Training:
{spacer}Positive count:          {train_pos_ct}
{spacer}Batch size:              {train_batch_size}
{spacer}Batch count per epoch:   {train_batch_ct}

Eval:
{spacer}Positive count:          {eval_pos_ct}
{spacer}Batch size:              {eval_batch_size}
{spacer}Batch count per epoch:   {eval_batch_ct}"""


class BaseDataConstructor(threading.Thread):
  def __init__(self,
               maximum_number_epochs,   # type: int
               num_users,               # type: int
               num_items,               # type: int
               train_pos_users,         # type: np.ndarray
               train_pos_items,         # type: np.ndarray
               train_batch_size,        # type: int
               batches_per_train_step,  # type: int
               num_train_negatives,     # type: int
               eval_pos_users,          # type: np.ndarray
               eval_pos_items,          # type: np.ndarray
               eval_batch_size,         # type: int
               batches_per_eval_step,   # type: int
              ):
    # General constants
    self._maximum_number_epochs = maximum_number_epochs
    self._num_users = num_users
    self._num_items = num_items

    # Training
    self._train_pos_users = train_pos_users
    self._train_pos_items = train_pos_items
    assert self._train_pos_users.shape == self._train_pos_items.shape
    self._train_pos_count = self._train_pos_users.shape[0]
    self._train_batch_size = train_batch_size
    self._num_train_negatives = num_train_negatives
    self._elements_in_epoch = (1 + num_train_negatives) * self._train_pos_count
    self._batches_per_train_step = batches_per_train_step
    self._train_batches_per_epoch = self._count_batches(
        self._elements_in_epoch, train_batch_size, batches_per_train_step)

    # Evaluation
    self._eval_pos_users = eval_pos_users
    self._eval_pos_items = eval_pos_items
    self._eval_batch_size = eval_batch_size
    if eval_batch_size % (1 + rconst.NUM_EVAL_NEGATIVES):
      raise ValueError("Eval batch size {} is not divisible by {}".format(
          eval_batch_size, 1 + rconst.NUM_EVAL_NEGATIVES))
    self._eval_users_per_batch = int(
        eval_batch_size // (1 + rconst.NUM_EVAL_NEGATIVES))
    self._eval_elements_in_epoch = num_users * (1 + rconst.NUM_EVAL_NEGATIVES)
    self._eval_batches_per_epoch = self._count_batches(
        self._eval_elements_in_epoch, eval_batch_size, batches_per_eval_step)

    # Intermediate artifacts
    self._current_epoch_order = None
    self._shuffle_producer = stat_utils.AsyncPermuter(
        self._elements_in_epoch, num_workers=3,
        num_to_produce=maximum_number_epochs+1)  # one extra for spillover.
    self._training_queue = queue.Queue()
    self._eval_results = None
    self._eval_batches = None

    # Threading details
    self._current_epoch_order_lock = threading.Lock()
    super(BaseDataConstructor, self).__init__()
    self.daemon = True
    self._stop_loop = False

  def __repr__(self):
    summary = SUMMARY_TEMPLATE.format(
        spacer="  ", num_users=self._num_users, num_items=self._num_items,
        train_pos_ct=self._train_pos_count,
        train_batch_size=self._train_batch_size,
        train_batch_ct=self._train_batches_per_epoch,
        eval_pos_ct=self._num_users, eval_batch_size=self._eval_batch_size,
        eval_batch_ct=self._eval_batches_per_epoch)
    return super(BaseDataConstructor, self).__repr__() + "\n" + summary

  def _count_batches(self, example_count, batch_size, batches_per_step):
    x = (example_count + batch_size - 1) // batch_size
    return (x + batches_per_step - 1) // batches_per_step * batches_per_step

  def stop_loop(self):
    self._shuffle_producer.stop_loop()
    self._stop_loop = True

  def _get_order_chunk(self):
    with self._current_epoch_order_lock:
      if self._current_epoch_order is None:
        self._current_epoch_order = self._shuffle_producer.get()

      batch_indices = self._current_epoch_order[:self._train_batch_size]
      self._current_epoch_order = self._current_epoch_order[self._train_batch_size:]

      if not self._current_epoch_order.shape[0]:
        self._current_epoch_order = self._shuffle_producer.get()

      num_extra = self._train_batch_size - batch_indices.shape[0]
      if num_extra:
        batch_indices = np.concatenate([batch_indices,
                                        self._current_epoch_order[:num_extra]])
        self._current_epoch_order = self._current_epoch_order[num_extra:]

      return batch_indices

  def construct_lookup_variables(self):
    raise NotImplementedError

  def lookup_negative_items(self, **kwargs):
    raise NotImplementedError

  def run(self):
    self._shuffle_producer.start()
    self.construct_lookup_variables()
    self._construct_training_epoch()
    self._construct_eval_epoch()
    for _ in range(self._maximum_number_epochs - 1):
      self._construct_training_epoch()

  def _get_training_batch(self, _):
    batch_indices = self._get_order_chunk()

    batch_ind_mod = np.mod(batch_indices, self._train_pos_count)
    users = self._train_pos_users[batch_ind_mod]

    negative_indices = np.greater_equal(batch_indices, self._train_pos_count)
    negative_users = users[negative_indices]

    negative_items = self.lookup_negative_items(
      batch_indices=batch_indices, batch_ind_mod=batch_ind_mod, users=users,
      negative_indices=negative_indices, negative_users=negative_users)

    items = self._train_pos_items[batch_ind_mod]
    items[negative_indices] = negative_items

    labels = np.logical_not(negative_indices).astype("int8")

    self._training_queue.put((users, items, labels))

  def _wait_to_construct_train_epoch(self):
    pass
    # spin_threshold = rconst.CYCLES_TO_BUFFER * self._train_batches_per_epoch
    # count = 0
    # while self._training_queue.qsize() >= spin_threshold:
    #   time.sleep(0.01)
    #   count += 1
    #   if count >= 100 and np.log10(count) == np.round(np.log10(count)):
    #     tf.logging.info(
    #         "Waited {} times for training data to be consumed".format(count))

  def _construct_training_epoch(self):
    self._wait_to_construct_train_epoch()

    start_time = timeit.default_timer()
    map_args = [i for i in range(self._train_batches_per_epoch)]
    with multiprocessing.dummy.Pool(6) as pool:
      pool.map(self._get_training_batch, map_args)

    tf.logging.info("Epoch construction complete. Time: {:.1f} seconds".format(
      timeit.default_timer() - start_time))

  def _get_eval_batch(self, i):
    low_index = i * self._eval_users_per_batch
    high_index = (i + 1) * self._eval_users_per_batch

    users = np.repeat(self._eval_pos_users[low_index:high_index, np.newaxis],
                      1 + rconst.NUM_EVAL_NEGATIVES, axis=1)

    # Ordering:
    #   The positive items should be last so that they lose ties. However, they
    #   should not be masked out if the true eval positive happens to be
    #   selected as a negative. So instead, the positive is placed in the first
    #   position, and then switched with the last element after the duplicate
    #   mask has been computed.
    items = np.concatenate([
      self._eval_pos_items[low_index:high_index, np.newaxis],
      self.lookup_negative_items(negative_users=users[:, :-1].flatten())
        .reshape(-1, rconst.NUM_EVAL_NEGATIVES),
    ], axis=1)

    duplicate_mask = stat_utils.mask_duplicates(items, axis=1)

    items[:, (0, -1)] = items[:, (-1, 0)]
    duplicate_mask[:, (0, -1)] = duplicate_mask[:, (-1, 0)]

    assert users.shape == items.shape == duplicate_mask.shape

    if users.shape[0] < self._eval_users_per_batch:
      pad_rows = self._eval_users_per_batch - users.shape[0]
      padding = np.zeros(shape=(pad_rows, users.shape[1]), dtype=np.int32)
      users = np.concatenate([users, padding.astype(users.dtype)], axis=0)
      items = np.concatenate([items, padding.astype(items.dtype)], axis=0)
      duplicate_mask = np.concatenate([duplicate_mask,
                                       padding.astype(users.dtype)], axis=0)

    return users.flatten(), items.flatten(), duplicate_mask.flatten()

  def _construct_eval_epoch(self):
    start_time = timeit.default_timer()
    map_args = [i for i in range(self._eval_batches_per_epoch)]
    with multiprocessing.dummy.Pool(6) as pool:
      eval_results = pool.map(self._get_eval_batch, map_args)

    self._eval_results = eval_results

    tf.logging.info("Eval construction complete. Time: {:.1f} seconds".format(
        timeit.default_timer() - start_time))

  def training_generator(self):
    for _ in range(self._train_batches_per_epoch):
      yield self._training_queue.get()

  def eval_generator(self):
    while self._eval_results is None:
      time.sleep(0.01)

    for i in self._eval_results:
      yield i


class MaterializedDataConstructor(BaseDataConstructor):
  def __init__(self, *args, **kwargs):
    super(MaterializedDataConstructor, self).__init__(*args, **kwargs)
    self._negative_table = None
    self._per_user_neg_count = None

  def construct_lookup_variables(self):
    # Materialize negatives for fast lookup sampling.
    start_time = timeit.default_timer()
    inner_bounds = np.argwhere(self._train_pos_users[1:] -
                               self._train_pos_users[:-1])[:, 0] + 1
    index_bounds = [0] + inner_bounds.tolist() + [self._num_users]
    self._negative_table = np.zeros(shape=(self._num_users, self._num_items),
                                    dtype=np.uint16)

    # Set the table to the max value to make sure the embedding lookup will fail
    # if we go out of bounds, rather than just overloading item zero.
    self._negative_table += np.iinfo(np.uint16).max
    assert self._num_items < np.iinfo(np.uint16).max

    # Reuse arange during generation. np.delete will make a copy.
    full_set = np.arange(self._num_items, dtype=np.uint16)

    self._per_user_neg_count = np.zeros(
      shape=(self._num_users,), dtype=np.int32)

    # Threading does not improve this loop. For some reason, the np.delete
    # call does not parallelize well. Multiprocessing incurs too much
    # serialization overhead to be worthwhile.
    for i in range(self._num_users):
      positives = self._train_pos_items[index_bounds[i]:index_bounds[i+1]]
      negatives = np.delete(full_set, positives)
      self._per_user_neg_count[i] = self._num_items - positives.shape[0]
      self._negative_table[i, :self._per_user_neg_count[i]] = negatives

    tf.logging.info("Negative sample table built. Time: {:.1f} seconds".format(
      timeit.default_timer() - start_time))

  def lookup_negative_items(self, negative_users, **kwargs):
    negative_item_choice = stat_utils.slightly_biased_randint(
      self._per_user_neg_count[negative_users])
    return self._negative_table[negative_users, negative_item_choice]






























def test_data_pipeline():
  data_file = "/tmp/MLPerf_NCF/movielens_data/1542303360_ncf_recommendation_cache/positives/positives.pickle"
  # num_users, num_items = 6040, 3706
  # num_train_pts = 994169

  num_users, num_items = 138493, 26744
  num_train_pts = 19861770

  with open(data_file, "rb") as f:
    data = pickle.load(f)

  # mat_gen = MaterializedGeneration(num_users, num_items, data, 100000)

  st = timeit.default_timer()

  mat_gen = MaterializedDataConstructor(
    15, num_users,  num_items,
    data[rconst.TRAIN_USER_KEY], data[rconst.TRAIN_ITEM_KEY], 1000000, 1, 4,
    data[rconst.EVAL_USER_KEY], data[rconst.EVAL_ITEM_KEY], 160000, 1
  )

  print(mat_gen)
  mat_gen.start()

  mat_gen.join()
  mat_gen.stop_loop()

  print("__", timeit.default_timer() - st)
  input("...")


if __name__ == "__main__":
  tf.logging.set_verbosity(tf.logging.INFO)
  test_data_pipeline()

