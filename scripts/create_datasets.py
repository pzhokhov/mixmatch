#!/usr/bin/env python

# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Script to download all datasets and create .tfrecord files.
"""

import collections
import gzip
import os
import sys
import tarfile
import tempfile
from urllib import request

from easydict import EasyDict
from libml.data import DATA_DIR
import numpy as np
import scipy.io
import tensorflow as tf
from tqdm import trange

URLS = {
    'svhn': 'http://ufldl.stanford.edu/housenumbers/{}_32x32.mat',
    'cifar10': 'https://www.cs.toronto.edu/~kriz/cifar-10-matlab.tar.gz',
    'cifar100': 'https://www.cs.toronto.edu/~kriz/cifar-100-matlab.tar.gz',
    'stl10': 'http://ai.stanford.edu/~acoates/stl10/stl10_binary.tar.gz',
}


def _encode_png(images):
    raw = []
    with tf.Session() as sess, tf.device('cpu:0'):
        image_x = tf.placeholder(tf.uint8, [None, None, None], 'image_x')
        to_png = tf.image.encode_png(image_x)
        for x in trange(images.shape[0], desc='PNG Encoding', leave=False):
            raw.append(sess.run(to_png, feed_dict={image_x: images[x]}))
    return raw


def _load_svhn():
    splits = collections.OrderedDict()
    for split in ['train', 'test', 'extra']:
        with tempfile.NamedTemporaryFile() as f:
            request.urlretrieve(URLS['svhn'].format(split), f.name)
            data_dict = scipy.io.loadmat(f.name)
        dataset = {}
        dataset['images'] = np.transpose(data_dict['X'], [3, 0, 1, 2])
        dataset['images'] = _encode_png(dataset['images'])
        dataset['labels'] = data_dict['y'].reshape((-1))
        # SVHN raw data uses labels from 1 to 10; use 0 to 9 instead.
        dataset['labels'] -= 1
        splits[split] = dataset
    return splits


def _load_stl10():
    def unflatten(images):
        return np.transpose(images.reshape((-1, 3, 96, 96)),
                            [0, 3, 2, 1])

    with tempfile.NamedTemporaryFile() as f:
        if os.path.exists('stl10/stl10_binary.tar.gz'):
            f = open('stl10/stl10_binary.tar.gz', 'rb')
        else:
            request.urlretrieve(URLS['stl10'], f.name)
        tar = tarfile.open(fileobj=f)
        train_X = tar.extractfile('stl10_binary/train_X.bin')
        train_y = tar.extractfile('stl10_binary/train_y.bin')

        test_X = tar.extractfile('stl10_binary/test_X.bin')
        test_y = tar.extractfile('stl10_binary/test_y.bin')

        unlabeled_X = tar.extractfile('stl10_binary/unlabeled_X.bin')

        train_set = {'images': np.frombuffer(train_X.read(), dtype=np.uint8),
                     'labels': np.frombuffer(train_y.read(), dtype=np.uint8) - 1}

        test_set = {'images': np.frombuffer(test_X.read(), dtype=np.uint8),
                    'labels': np.frombuffer(test_y.read(), dtype=np.uint8) - 1}

        _imgs = np.frombuffer(unlabeled_X.read(), dtype=np.uint8)
        unlabeled_set = {'images': _imgs,
                         'labels': np.zeros(100000, dtype=np.uint8)}

        fold_indices = tar.extractfile('stl10_binary/fold_indices.txt').read()

    train_set['images'] = _encode_png(unflatten(train_set['images']))
    test_set['images'] = _encode_png(unflatten(test_set['images']))
    unlabeled_set['images'] = _encode_png(unflatten(unlabeled_set['images']))
    return dict(train=train_set, test=test_set, unlabeled=unlabeled_set,
                files=[EasyDict(filename="stl10_fold_indices.txt", data=fold_indices)])


def _load_cifar10():
    def unflatten(images):
        return np.transpose(images.reshape((images.shape[0], 3, 32, 32)),
                            [0, 2, 3, 1])

    with tempfile.NamedTemporaryFile() as f:
        request.urlretrieve(URLS['cifar10'], f.name)
        tar = tarfile.open(fileobj=f)
        train_data_batches, train_data_labels = [], []
        for batch in range(1, 6):
            data_dict = scipy.io.loadmat(tar.extractfile(
                'cifar-10-batches-mat/data_batch_{}.mat'.format(batch)))
            train_data_batches.append(data_dict['data'])
            train_data_labels.append(data_dict['labels'].flatten())
        train_set = {'images': np.concatenate(train_data_batches, axis=0),
                     'labels': np.concatenate(train_data_labels, axis=0)}
        data_dict = scipy.io.loadmat(tar.extractfile(
            'cifar-10-batches-mat/test_batch.mat'))
        test_set = {'images': data_dict['data'],
                    'labels': data_dict['labels'].flatten()}
    train_set['images'] = _encode_png(unflatten(train_set['images']))
    test_set['images'] = _encode_png(unflatten(test_set['images']))
    return dict(train=train_set, test=test_set)

def _load_coinrun320k():
    import joblib
    data = []
    for rank in range(1):
        data.append(joblib.load(_download_datafile_if_necessary(f'traj_st400_80k_{rank}.gz')))
    data = {k: np.concatenate([d[k] for d in data], axis=0) for k in data[0].keys()}
    ntest = 20000
    train_set = {'images': _encode_png(data['obs'][:-ntest]), 'labels': data['actions'][:-ntest]}
    test_set = {'images': _encode_png(data['obs'][ntest:]), 'labels': data['actions'][ntest:]}
    return dict(train=train_set, test=test_set)

def _download_datafile_if_necessary(filename):
    datafile_dir = os.path.expanduser('~/data/workbench/')
    tf.io.gfile.makedirs(datafile_dir)
    files = os.listdir(datafile_dir)
    filepath = os.path.join(datafile_dir, filename)
    gcs_datadir = 'gs://peterz-rcall'
    gspath = os.path.join(f'{gcs_datadir}/workbench/data', filename)
    if filename not in files:
        print(f'downloading from {gspath}...')
        tf.io.gfile.copy(gspath, filepath, overwrite=True)
        print(f'downloaded {filename}.')
    return filepath

def _load_cifar100():
    def unflatten(images):
        return np.transpose(images.reshape((images.shape[0], 3, 32, 32)),
                            [0, 2, 3, 1])

    with tempfile.NamedTemporaryFile() as f:
        request.urlretrieve(URLS['cifar100'], f.name)
        tar = tarfile.open(fileobj=f)
        data_dict = scipy.io.loadmat(tar.extractfile('cifar-100-matlab/train.mat'))
        train_set = {'images': data_dict['data'],
                     'labels': data_dict['fine_labels'].flatten()}
        data_dict = scipy.io.loadmat(tar.extractfile('cifar-100-matlab/test.mat'))
        test_set = {'images': data_dict['data'],
                    'labels': data_dict['fine_labels'].flatten()}
    train_set['images'] = _encode_png(unflatten(train_set['images']))
    test_set['images'] = _encode_png(unflatten(test_set['images']))
    return dict(train=train_set, test=test_set)


def _int64_feature(value):
    return tf.train.Feature(int64_list=tf.train.Int64List(value=[value]))


def _bytes_feature(value):
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def _save_as_tfrecord(data, filename):
    assert len(data['images']) == len(data['labels'])
    filename = os.path.join(DATA_DIR, filename + '.tfrecord')
    print('Saving dataset:', filename)
    with tf.python_io.TFRecordWriter(filename) as writer:
        for x in trange(len(data['images']), desc='Building records'):
            feat = dict(image=_bytes_feature(data['images'][x]),
                        label=_int64_feature(data['labels'][x]))
            record = tf.train.Example(features=tf.train.Features(feature=feat))
            writer.write(record.SerializeToString())
    print('Saved:', filename)


def _is_installed(name, checksums):
    for subset, checksum in checksums.items():
        filename = os.path.join(DATA_DIR, '%s-%s.tfrecord' % (name, subset))
        if not os.path.exists(filename):
            return False
    return True


def _save_files(files, *args, **kwargs):
    del args, kwargs
    for folder in frozenset(os.path.dirname(x) for x in files):
        os.makedirs(os.path.join(DATA_DIR, folder), exist_ok=True)
    for filename, contents in files.items():
        with open(os.path.join(DATA_DIR, filename), 'w') as f:
            f.write(contents)


def _is_installed_folder(name, folder):
    return os.path.exists(os.path.join(DATA_DIR, name, folder))


CONFIGS = dict(
    cifar10=dict(loader=_load_cifar10,
                 checksums=dict(train=None, test=None)),
    cifar100=dict(loader=_load_cifar100,
                  checksums=dict(train=None, test=None)),
    svhn=dict(loader=_load_svhn,
              checksums=dict(train=None, test=None, extra=None)),
    stl10=dict(loader=_load_stl10,
               checksums=dict(train=None, test=None)),
    coinrun320k=dict(loader=_load_coinrun320k, checksums=dict(train=None, test=None))
)

if __name__ == '__main__':
    if len(sys.argv[1:]):
        subset = set(sys.argv[1:])
    else:
        subset = set(CONFIGS.keys())
    try:
        os.makedirs(DATA_DIR)
    except OSError:
        pass
    for name, config in CONFIGS.items():
        if name not in subset:
            continue
        if 'is_installed' in config:
            if config['is_installed']():
                print('Skipping already installed:', name)
                continue
        elif _is_installed(name, config['checksums']):
            print('Skipping already installed:', name)
            continue
        print('Preparing', name)
        datas = config['loader']()
        saver = config.get('saver', _save_as_tfrecord)
        for sub_name, data in datas.items():
            if sub_name == 'readme':
                filename = os.path.join(DATA_DIR, '%s-%s.txt' % (name, sub_name))
                with open(filename, 'w') as f:
                    f.write(data)
            elif sub_name == 'files':
                for file_and_data in data:
                    path = os.path.join(DATA_DIR, file_and_data.filename)
                    open(path, "wb").write(file_and_data.data)
            else:
                saver(data, '%s-%s' % (name, sub_name))
