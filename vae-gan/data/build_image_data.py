# Copyright 2017 Google Inc. All Rights Reserved.
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
"""Converts image data to TFRecords file format with Example protos.

The image data set is expected to reside in JPEG files located in the
following directory structure.

  data_directory/image0.jpeg
  data_directory/image1.jpg
  ...
  data_directory/weird-image.jpeg
  data_directory/my-image.jpeg
  ...

where the data_directory contains all images in the dataset.

This TensorFlow script converts the data into a sharded training dataset and a
sharded validation dataset consisting of TFRecord files

  output_directory/train-00000-of-01024
  output_directory/train-00001-of-01024
  ...
  output_directory/train-001023-of-01024

and

  output_directory/validation-00000-of-00128
  output_directory/validation-00001-of-00128
  ...
  output_directory/validation-00127-of-00128

where we have selected 1024 and 128 shards for each data set. Each record
within the TFRecord file is a serialized Example proto. The Example proto
contains the following fields:

  image/encoded: string containing JPEG encoded image in RGB colorspace
  image/height: integer, image height in pixels
  image/width: integer, image width in pixels
  image/colorspace: string, specifying the colorspace, always 'RGB'
  image/channels: integer, specifying the number of channels, always 3
  image/format: string, specifying the format, always'JPEG'

  image/filename: string containing the basename of the image file
            e.g. 'n01440764_10026.JPEG' or 'ILSVRC2012_val_00000293.JPEG'
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import logging
import os
import random
import sys
import threading

import numpy as np
import tensorflow as tf

tf.app.flags.DEFINE_string('data_directory', None, 'Data directory')
tf.app.flags.DEFINE_string('output_directory', '', 'Output data directory')
tf.app.flags.DEFINE_integer('num_shards', 2,
                            'Number of shards in TFRecord files.')
tf.app.flags.DEFINE_integer('num_threads', 2,
                            'Number of threads to preprocess the images.')
tf.app.flags.DEFINE_float('validation_size', 0.1, 'Size of validation set in proportion to whole.')
FLAGS = tf.app.flags.FLAGS



def _int64_feature(value):
  """Wrapper for inserting int64 features into Example proto."""
  if not isinstance(value, list):
    value = [value]
  return tf.train.Feature(int64_list=tf.train.Int64List(value=value))


def _bytes_feature(value):
  """Wrapper for inserting bytes features into Example proto."""
  return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def _convert_to_example(filename, image_buffer, height, width, channels):
  """Build an Example proto for an example.

  Args:
    filename: string, path to an image file, e.g., '/path/to/example.JPG'
    image_buffer: string, JPEG encoding of RGB image
    height: integer, image height in pixels
    width: integer, image width in pixels
  Returns:
    Example proto
  """
  colorspace = 'RGBA' if channels == 4 else 'RGB' if channels == 3 else 'MONO' if channels == 1 else 'unknown'
  image_format = 'PNG'

  example = tf.train.Example(features=tf.train.Features(feature={
      'image/height':
          _int64_feature(height),
      'image/width':
          _int64_feature(width),
      'image/colorspace':
          _bytes_feature(tf.compat.as_bytes(colorspace)),
      'image/channels':
          _int64_feature(channels),
      'image/format':
          _bytes_feature(tf.compat.as_bytes(image_format)),
      'image/filename':
          _bytes_feature(tf.compat.as_bytes(os.path.basename(filename))),
      'image/encoded':
          _bytes_feature(tf.compat.as_bytes(image_buffer))
  }))
  return example


class ImageCoder(object):
  """Helper class that provides TensorFlow image coding utilities."""

  def __init__(self):
    # Create a single Session to run all image coding calls.
    self._sess = tf.Session()

    # Initializes function that decodes PNG data.
    self._decode_png_data = tf.placeholder(dtype=tf.string)
    self._decode_png = tf.image.decode_png(self._decode_png_data)

  def decode_png(self, image_data):
    image = self._sess.run(
        self._decode_png, feed_dict={self._decode_png_data: image_data})
    assert len(image.shape) == 3
    return image


def _is_png(filename):
  """Determine if a file contains a PNG format image.

  Args:
    filename: string, path of the image file.

  Returns:
    boolean indicating if the image is a PNG.
  """
  return '.png' in filename


def _process_image(filename, coder):
  """Process a single image file.

  Args:
    filename: string, path to an image file e.g., '/path/to/example.JPG'.
    coder: instance of ImageCoder to provide TensorFlow image coding utils.
  Returns:
    image_buffer: string, JPEG encoding of RGB image.
    height: integer, image height in pixels.
    width: integer, image width in pixels.
  """
  # Read the image file.
  image_data = tf.gfile.FastGFile(filename, 'rb').read()

  # Decode the RGB PNG.
  image = coder.decode_png(image_data)

  # Check that image converted to RGB
  height = image.shape[0]
  width = image.shape[1]
  channels = image.shape[2]

  return image_data, height, width, channels


def _process_image_files_batch(coder, thread_index, ranges, name, filenames,
                               num_shards):
  """Processes and saves list of images as TFRecord in 1 thread.

  Args:
    coder: instance of ImageCoder to provide TensorFlow image coding utils.
    thread_index: integer, unique batch to run index is within [0, len(ranges)).
    ranges: list of pairs of integers specifying ranges of each batches to
      analyze in parallel.
    name: string, unique identifier specifying the data set
    filenames: list of strings; each string is a path to an image file
    num_shards: integer number of shards for this data set.
  """
  # Each thread produces N shards where N = int(num_shards / num_threads).
  # For instance, if num_shards = 128, and the num_threads = 2, then the first
  # thread would produce shards [0, 64).
  num_threads = len(ranges)
  assert not num_shards % num_threads
  num_shards_per_batch = int(num_shards / num_threads)

  shard_ranges = np.linspace(ranges[thread_index][0], ranges[thread_index][1],
                             num_shards_per_batch + 1).astype(int)
  num_files_in_thread = ranges[thread_index][1] - ranges[thread_index][0]

  counter = 0
  for s in range(num_shards_per_batch):
    # Generate a sharded version of the file name, e.g. 'train-00002-of-00010'
    shard = thread_index * num_shards_per_batch + s
    output_filename = '%s-%.5d-of-%.5d' % (name, shard, num_shards)
    output_file = os.path.join(FLAGS.output_directory, output_filename)
    writer = tf.python_io.TFRecordWriter(output_file)

    shard_counter = 0
    files_in_shard = np.arange(shard_ranges[s], shard_ranges[s + 1], dtype=int)
    for i in files_in_shard:
      filename = filenames[i]

      image_buffer, height, width, channels = _process_image(filename, coder)

      example = _convert_to_example(filename, image_buffer, height, width, channels)
      writer.write(example.SerializeToString())
      shard_counter += 1
      counter += 1

      if not counter % 1000:
        logging.info(
            '%s [thread %d]: Processed %d of %d images in thread batch.',
            datetime.now(), thread_index, counter, num_files_in_thread)
        sys.stdout.flush()

    writer.close()
    logging.info('%s [thread %d]: Wrote %d images to %s',
                 datetime.now(), thread_index, shard_counter, output_file)
    sys.stdout.flush()
    shard_counter = 0
  logging.info('%s [thread %d]: Wrote %d images to %d shards.',
               datetime.now(), thread_index, counter, num_files_in_thread)
  sys.stdout.flush()


def _process_image_files(name, filenames, num_shards):
  """Process and save list of images as TFRecord of Example protos.

  Args:
    name: string, unique identifier specifying the data set
    filenames: list of strings; each string is a path to an image file
    num_shards: integer number of shards for this data set.
  """

  # Break all images into batches with a [ranges[i][0], ranges[i][1]].
  spacing = np.linspace(0, len(filenames), FLAGS.num_threads + 1).astype(np.int)
  ranges = []
  threads = []
  for i in range(len(spacing) - 1):
    ranges.append([spacing[i], spacing[i + 1]])

  # Launch a thread for each batch.
  logging.info('Launching %d threads for spacings: %s', FLAGS.num_threads,
               ranges)
  sys.stdout.flush()

  # Create a mechanism for monitoring when all threads are finished.
  coord = tf.train.Coordinator()

  # Create a generic TensorFlow-based utility for converting all image codings.
  coder = ImageCoder()

  threads = []
  for thread_index in range(len(ranges)):
    args = (coder, thread_index, ranges, name, filenames, num_shards)
    t = threading.Thread(target=_process_image_files_batch, args=args)
    t.start()
    threads.append(t)

  # Wait for all the threads to terminate.
  coord.join(threads)
  logging.info('%s: Finished writing all %d images in data set.',
               datetime.now(), len(filenames))
  sys.stdout.flush()


def _find_image_files(data_dir):
  """Build a list of all images files and labels in the data set.

  Args:
    data_dir: string, path to the root directory of images.

      Assumes that the image data set resides in JPEG files located in
      the following directory structure.

        data_dir/image.jpg


  Returns:
    filenames: list of strings; each string is a path to an image file.
  """
  filenames = []

  file_extensions = ['.png']
  file_extensions += [ext.upper() for ext in file_extensions]

  for ext in file_extensions:
    file_path = '%s/*%s' % (data_dir, ext)
    matching_files = tf.gfile.Glob(file_path)
    filenames.extend(matching_files)

  # Shuffle the ordering of all image files in order to guarantee
  # random ordering of the images saved in the TFRecord files.
  # Make the randomization repeatable.
  shuffled_index = list(range(len(filenames)))
  random.seed(12345)
  random.shuffle(shuffled_index)

  filenames = [filenames[i] for i in shuffled_index]

  logging.info('Found %d image files inside %s.', len(filenames), data_dir)
  return filenames


def _process_datasets(directory, num_shards):
  """Process a complete data set and save it as a TFRecord.

  Args:
    directory: string, root path to the data set.
    num_shards: integer number of shards for this data set.
  """
  filenames = _find_image_files(directory)

  num_train_files = int(len(filenames) * (1 - FLAGS.validation_size))
  num_validation_files = len(filenames) - num_train_files

  if num_train_files != 0:
    train_filenames = filenames[:num_train_files]
    _process_image_files('train', train_filenames, num_shards)

  if num_validation_files != 0:
    validation_filenames = filenames[-num_validation_files:]
    _process_image_files('validation', validation_filenames, num_shards)


def main(unused_argv):
  assert not FLAGS.num_shards % FLAGS.num_threads, (
      'Please make the FLAGS.num_threads commensurate with FLAGS.num_shards')
  logging.info('Saving results to %s', FLAGS.output_directory)
  _process_datasets(FLAGS.data_directory, FLAGS.num_shards)


if __name__ == '__main__':
  logging.basicConfig(level=logging.INFO)
  tf.app.run()
