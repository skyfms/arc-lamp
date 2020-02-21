#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
  arclamp-log
  ~~~~~~~~~

  This Arc Lamp components reads strack traces from a Redis channel,
  and writes them to one or more log files, organised by date
  and application entry point.

"""
from __future__ import print_function

import sys
reload(sys)
sys.setdefaultencoding('utf-8')

import argparse
import datetime
import errno
import fnmatch
import os
import os.path
import re

import redis
import yaml


parser = argparse.ArgumentParser()
# Configuration keys:
#
# base_path: Directory in which files should be created. [optional]
#            Default: "/srv/arclamp/logs".
#
# redis: Parameters to establish a connection using python-redis.
#   host: [required]
#   port: [required]
#
# redis_channel: The name of the Redis PubSub channel to subscribe to. [optional]
#                Default: "arclamp".
#
# logs: A list of one or more log file groups. [required]
#
#       Each log group has a date-time string that informs how much time a
#       single file in the group represents (e.g. an hour or a day), and what
#       pattern to use for the file name.
#
#       format: Format string for Python strftime. This informs both the
#               time aggregation and the filename.
#
#               The formatted time and the suffix ".{tag}.log" together
#               form the log file name. The "tag" represents the application
#               entry point. All traces are also written to a second file,
#               with the tag "all", which combines all entry points.
#               The tag is determined by the first frame of the stack trace.
#               For example, a stack "index.php;main;Stuff::doIt 1" will be
#               written to "{format}.all.log" and "{format}.index.log".
#
#       period: Directory name. The files formatted by 'format' will be
#               placed in a sub directory of 'base_path' by this name.
#       retain: How many files to keep in the 'period' directory for
#               a single application entry point. Once this has been exceeded,
#               files exceeding this limit will be removed (oldest first).
#
parser.add_argument('config', nargs='?', default='/etc/arclamp-log.yaml')
args = parser.parse_args()

with open(args.config) as f:
    config = yaml.safe_load(f)


class TimeLog(object):

    base_path = config.get('base_path', '/srv/arclamp/logs')

    def __init__(self, period, format, retain, write_every=1):
        self.period = period
        self.format = format
        self.retain = retain
        self.write_every = write_every
        self.skipped = 0
        self.path = os.path.join(self.base_path, period)
        try:
            os.makedirs(self.path, 0755)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise

    def write(self, message, time=None, tag='all'):
        self.skipped += 1
        if self.skipped >= self.write_every:
            self.skipped = 0
            time = datetime.datetime.utcnow() if time is None else time
            base_name = '%s.%s.log' % (time.strftime(self.format), tag)
            file_path = os.path.join(self.path, base_name)
            if not os.path.isfile(file_path):
                self.prune_files(tag)
            # T169249 buffering=1 makes it line-buffered
            with open(file_path, mode='a', buffering=1) as f:
                print(message, file=f)

    def prune_files(self, tag):
        mask = '*.%s.log' % tag
        files = {}
        for base_name in os.listdir(self.path):
            if not fnmatch.fnmatch(base_name, mask):
                continue
            file_path = os.path.join(self.path, base_name)
            try:
                files[file_path] = os.path.getctime(file_path)
            except ValueError:
                continue
        files = list(sorted(files, key=files.get, reverse=True))
        for file_path in files[self.retain:]:
            try:
                os.remove(file_path)
            except OSError:
                continue


logs = [TimeLog(**log) for log in config['logs']]
conn = redis.Redis(**config['redis'])
pubsub = conn.pubsub()
pubsub.subscribe(config.get('redis_channel', 'arclamp'))


def get_tag(raw_stack):
    m = re.match(r'(?:[^;]+/)*(\w+).php', raw_stack)
    return m.group(1) if m else None


for message in pubsub.listen():
    # T169249 skip the subscription confirmation message
    if message['type'] != 'message':
        continue

    data = message['data']
    time = datetime.datetime.utcnow()
    tag = get_tag(str(data))
    for log in logs:
        log.write(data, time, 'all')
        if tag:
            log.write(data, time, tag)
