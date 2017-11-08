import hashlib
from io import StringIO
from datetime import timedelta
import re
import random
import string
import struct
import time
import sys
import shutil
import subprocess
import os
import numpy as np
import requests
import six

from tensorflow.core.util import event_pb2

import boto3

DAY = 86400
HOUR = 3600
MINUTE = 60


def remove_backspaces(line):
    splitline = re.split('(\x08+)', line)
    try:
        splitline = [unicode(s, 'utf-8') for s in splitline]
    except NameError:
        splitline = [str(s) for s in splitline]

    buf = StringIO()
    for i in range(0, len(splitline) - 1, 2):
        buf.write(splitline[i][:-len(splitline[i + 1])])

    if len(splitline) % 2 == 1:
        buf.write(splitline[-1])

    return buf.getvalue()


def sha256_checksum(filename, block_size=65536):
    sha256 = hashlib.sha256()
    with open(filename, 'rb') as f:
        for block in iter(lambda: f.read(block_size), b''):
            sha256.update(block)
    return sha256.hexdigest()


def rand_string(length):
    return "".join([random.choice(string.ascii_letters + string.digits)
                    for n in range(length)])


def event_reader(fileobj):

    if isinstance(fileobj, str):
        fileobj = open(fileobj, 'rb')

    header_len = 12
    footer_len = 4
    size_len = 8

    while True:
        try:
            data_len = struct.unpack('Q', fileobj.read(size_len))[0]
            fileobj.read(header_len - size_len)

            data = fileobj.read(data_len)

            event = None
            event = event_pb2.Event()
            event.ParseFromString(data)

            fileobj.read(footer_len)
            yield event
        except BaseException:
            break

    fileobj.close()


def rsync_cp(source, dest, ignore_arg='', logger=None):
    if os.path.exists(dest):
        shutil.rmtree(dest) if os.path.isdir(dest) else os.remove(dest)
    os.makedirs(dest)

    if ignore_arg != '':
        source += "/"
        tool = 'rsync'
        args = [tool, ignore_arg, '-aHAXE', source, dest]
    else:
        os.rmdir(dest)
        tool = 'cp'
        args = [tool, '-pR', source, dest]

    pcp = subprocess.Popen(args, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
    cpout, _ = pcp.communicate()
    if pcp.returncode != 0 and logger is not None:
        logger.info('%s returned non-zero exit code. Output:' % tool)
        logger.info(cpout)


class Progbar(object):
    """Displays a progress bar.

    # Arguments
        target: Total number of steps expected, None if unknown.
        interval: Minimum visual progress update interval (in seconds).
    """

    def __init__(self, target, width=30, verbose=1, interval=0.05):
        self.width = width
        if target is None:
            target = -1
        self.target = target
        self.sum_values = {}
        self.unique_values = []
        self.start = time.time()
        self.last_update = 0
        self.interval = interval
        self.total_width = 0
        self.seen_so_far = 0
        self.verbose = verbose

    def update(self, current, values=None, force=False):
        """Updates the progress bar.

        # Arguments
            current: Index of current step.
            values: List of tuples (name, value_for_last_step).
                The progress bar will display averages for these values.
            force: Whether to force visual progress update.
        """
        values = values or []
        for k, v in values:
            if k not in self.sum_values:
                self.sum_values[k] = [v * (current - self.seen_so_far),
                                      current - self.seen_so_far]
                self.unique_values.append(k)
            else:
                self.sum_values[k][0] += v * (current - self.seen_so_far)
                self.sum_values[k][1] += (current - self.seen_so_far)
        self.seen_so_far = current

        now = time.time()
        if self.verbose == 1:
            if not force and (now - self.last_update) < self.interval:
                return

            prev_total_width = self.total_width
            sys.stdout.write('\b' * prev_total_width)
            sys.stdout.write('\r')

            if self.target is not -1:
                numdigits = int(np.floor(np.log10(self.target))) + 1
                barstr = '%%%dd/%%%dd [' % (numdigits, numdigits)
                bar = barstr % (current, self.target)
                prog = float(current) / self.target
                prog_width = int(self.width * prog)
                if prog_width > 0:
                    bar += ('=' * (prog_width - 1))
                    if current < self.target:
                        bar += '>'
                    else:
                        bar += '='
                bar += ('.' * (self.width - prog_width))
                bar += ']'
                sys.stdout.write(bar)
                self.total_width = len(bar)

            if current:
                time_per_unit = (now - self.start) / current
            else:
                time_per_unit = 0
            eta = time_per_unit * (self.target - current)
            info = ''
            if current < self.target and self.target is not -1:
                info += ' - ETA: %ds' % eta
            else:
                info += ' - %ds' % (now - self.start)
            for k in self.unique_values:
                info += ' - %s:' % k
                if isinstance(self.sum_values[k], list):
                    avg = np.mean(
                        self.sum_values[k][0] / max(1, self.sum_values[k][1]))
                    if abs(avg) > 1e-3:
                        info += ' %.4f' % avg
                    else:
                        info += ' %.4e' % avg
                else:
                    info += ' %s' % self.sum_values[k]

            self.total_width += len(info)
            if prev_total_width > self.total_width:
                info += ((prev_total_width - self.total_width) * ' ')

            sys.stdout.write(info)
            sys.stdout.flush()

            if current >= self.target:
                sys.stdout.write('\n')

        if self.verbose == 2:
            if current >= self.target:
                info = '%ds' % (now - self.start)
                for k in self.unique_values:
                    info += ' - %s:' % k
                    avg = np.mean(
                        self.sum_values[k][0] / max(1, self.sum_values[k][1]))
                    if avg > 1e-3:
                        info += ' %.4f' % avg
                    else:
                        info += ' %.4e' % avg
                sys.stdout.write(info + "\n")

        self.last_update = now

    def add(self, n, values=None):
        self.update(self.seen_so_far + n, values)


def download_file(url, local_path, logger=None):
    response = requests.get(
        url,
        stream=True)
    if logger:
        logger.info(("Trying to download file at url {} to " +
                     "local path {}").format(url, local_path))

    if response.status_code == 200:
        with open(local_path, 'wb') as f:
            for chunk in response:
                f.write(chunk)
    elif logger:
        logger.info("Response error with code {}"
                    .format(response.status_code))

    return response


def download_file_from_qualified(qualified, local_path, logger=None):
    assert qualified.startswith('s3://') or \
        qualified.startswith('gs://')

    bucket = qualified.split('/')[2]
    key = '/'.join(qualified.split('/')[3:])

    if logger is not None:
        logger.debug(('Downloading file from bucket {} ' +
                      ' and key {} to local path {}')
                     .format(bucket, key, local_path))

    if qualified.startswith('s3://'):
        boto3.client('s3').download_file(bucket, key, local_path)
    else:
        raise NotImplementedError


def has_aws_credentials():
    return boto3.client('s3')._request_signer._credentials is not None


def retry(f,
          no_retries=5, sleeptime=1,
          exception_class=BaseException, logger=None):
    for i in range(no_retries):
        try:
            return f()
        except exception_class as e:
            if logger:
                logger.info(
                    ('Exception {} is caught, ' +
                     'sleeping {}s and retrying (attempt {} of {})')
                    .format(e, sleeptime, i, no_retries))
            time.sleep(sleeptime)


def compression_to_extension(compression):
    return _compression_to_extension_taropt(compression)[0]


def compression_to_taropt(compression):
    return _compression_to_extension_taropt(compression)[1]


def _compression_to_extension_taropt(compression):
    default_compression = 'none'
    if compression is None:
        compression = default_compression

    compression = compression.lower()

    if compression == 'bzip2':
        return '.bz2', '--bzip2'

    elif compression == 'gzip':
        return '.gz', '--gzip'

    elif compression == 'xz':
        return '.xz', '--xz'

    elif compression == 'lzma':
        return '.lzma', '--lzma'

    elif compression == 'lzop':
        return '.lzop', '--lzop'

    elif compression == 'none':
        return '', ''

    raise ValueError('Unknown compression method {}'
                     .format(compression))


def timeit(method):

    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()

        line = '%r (%r, %r) %2.2f sec' % \
            (method.__name__, args, kw, te - ts)

        try:
            logger = args[0].logger
            logger.info(line)
        except BaseException:
            print(line)

        return result

    return timed


def sixdecode(s):
    if isinstance(s, six.string_types):
        return s
    if isinstance(s, six.binary_type):
        return s.decode('utf8')

    raise TypeError("Unknown type of " + str(s))


regex = re.compile(
    r'((?P<hours>\d+?)h)?((?P<minutes>\d+?)m)?((?P<seconds>\d+?)s)?')


# parse_duration parses strings into time delta values that python can
# deal with.  Examples include 12h, 11h60m, 719m60s, 11h3600s
#
def parse_duration(duration_str):
    parts = regex.match(duration_str)
    if not parts:
        return
    parts = parts.groupdict()
    time_params = {}
    for (name, param) in parts.iteritems():
        if param:
            time_params[name] = int(param)
    return timedelta(**time_params)


def str2duration(s):
    return parse_duration(s.lower())
