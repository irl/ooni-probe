# -*- coding: utf-8 -*-

from ooni.oonibclient import OONIBClient
from ooni.nettest import NetTestLoader
from ooni.settings import config
from ooni.utils import log
from ooni import errors as e

from twisted import version as _twisted_version
from twisted.python.versions import Version

from twisted.python.filepath import FilePath
from twisted.internet import defer

import os
import yaml
import json
from hashlib import sha256


class InputFile(object):
    def __init__(self, input_hash, base_path=config.inputs_directory):
        self.id = input_hash
        cache_path = os.path.join(os.path.abspath(base_path), input_hash)
        self.cached_file = cache_path
        self.cached_descriptor = cache_path + '.desc'

    @property
    def descriptorCached(self):
        if os.path.exists(self.cached_descriptor):
            with open(self.cached_descriptor) as f:
                descriptor = json.load(f)
                self.load(descriptor)
            return True
        return False

    @property
    def fileCached(self):
        if os.path.exists(self.cached_file):
            try:
                self.verify()
            except AssertionError:
                log.err("The input %s failed validation."
                        "Going to consider it not cached." % self.id)
                return False
            return True
        return False

    def save(self):
        with open(self.cached_descriptor, 'w+') as f:
            json.dump({
                'name': self.name,
                'id': self.id,
                'version': self.version,
                'author': self.author,
                'date': self.date,
                'description': self.description
            }, f)

    def load(self, descriptor):
        self.name = descriptor['name']
        self.version = descriptor['version']
        self.author = descriptor['author']
        self.date = descriptor['date']
        self.description = descriptor['description']

    def verify(self):
        digest = os.path.basename(self.cached_file)
        with open(self.cached_file) as f:
            file_hash = sha256(f.read())
            assert file_hash.hexdigest() == digest


def nettest_to_path(path, allow_arbitrary_paths=False):
    """
    Takes as input either a path or a nettest name.

    Args:

        allow_arbitrary_paths:
            allow also paths that are not relative to the nettest_directory.

    Returns:

        full path to the nettest file.
    """
    if allow_arbitrary_paths and os.path.exists(path):
        return path

    fp = FilePath(config.nettest_directory).preauthChild(path + '.py')
    if fp.exists():
        return fp.path
    else:
        raise e.NetTestNotFound(path)


class Deck(InputFile):
    def __init__(self, deck_hash=None,
                 deckFile=None,
                 decks_directory=config.decks_directory,
                 no_collector=False):
        self.id = deck_hash
        self.requiresTor = False
        self.no_collector = no_collector
        self.bouncer = ''
        self.netTestLoaders = []
        self.inputs = []

        self.oonibclient = OONIBClient(self.bouncer)

        self.decksDirectory = os.path.abspath(decks_directory)
        self.deckHash = deck_hash

        if deckFile:
            self.loadDeck(deckFile)

    @property
    def cached_file(self):
        return os.path.join(self.decksDirectory, self.deckHash)

    @property
    def cached_descriptor(self):
        return self.cached_file + '.desc'

    def loadDeck(self, deckFile):
        with open(deckFile) as f:
            self.deckHash = sha256(f.read()).hexdigest()
            f.seek(0)
            test_deck = yaml.safe_load(f)

        for test in test_deck:
            try:
                nettest_path = nettest_to_path(test['options']['test_file'])
            except e.NetTestNotFound:
                log.err("Could not find %s" % test['options']['test_file'])
                log.msg("Skipping...")
                continue
            net_test_loader = NetTestLoader(test['options']['subargs'],
                                            annotations=test['options'].get('annotations', {}),
                                            test_file=nettest_path)
            if test['options']['collector']:
                net_test_loader.collector = test['options']['collector']
            self.insert(net_test_loader)

    def insert(self, net_test_loader):
        """ Add a NetTestLoader to this test deck """
        try:
            net_test_loader.checkOptions()
            if net_test_loader.requiresTor:
                self.requiresTor = True
        except e.MissingTestHelper:
            if not self.bouncer:
                raise
            self.requiresTor = True

        if net_test_loader.collector and net_test_loader.collector.startswith('https://'):
            _twisted_14_0_2_version = Version('twisted', 14, 0, 2)
            if _twisted_version < _twisted_14_0_2_version:
                raise e.HTTPCollectorUnsupported
        elif net_test_loader.collector and net_test_loader.collector.startswith('http://'):
            if config.advanced.insecure_collector is not True:
                raise e.InsecureCollector
        self.netTestLoaders.append(net_test_loader)

    @defer.inlineCallbacks
    def setup(self):
        """ fetch and verify inputs for all NetTests in the deck """
        log.msg("Fetching required net test inputs...")
        for net_test_loader in self.netTestLoaders:
            yield self.fetchAndVerifyNetTestInput(net_test_loader)

        if self.bouncer:
            log.msg("Looking up collector and test helpers")
            yield self.lookupCollector()

    @defer.inlineCallbacks
    def lookupCollector(self):
        self.oonibclient.address = self.bouncer

        required_nettests = []

        requires_test_helpers = False
        requires_collector = False
        for net_test_loader in self.netTestLoaders:
            nettest = {
                'name': net_test_loader.testName,
                'version': net_test_loader.testVersion,
                'test-helpers': [],
                'input-hashes': [x['hash'] for x in net_test_loader.inputFiles]
            }
            if not net_test_loader.collector and not self.no_collector:
                requires_collector = True

            if len(net_test_loader.missingTestHelpers) > 0:
                requires_test_helpers = True
                nettest['test-helpers'] += map(lambda x: x[1],
                                               net_test_loader.missingTestHelpers)

            required_nettests.append(nettest)

        if not requires_test_helpers and not requires_collector:
            defer.returnValue(None)

        log.debug("Looking up {}".format(required_nettests))
        response = yield self.oonibclient.lookupTestCollector(required_nettests)
        provided_net_tests = response['net-tests']

        def find_collector_and_test_helpers(test_name, test_version, input_files):
            input_files = [u""+x['hash'] for x in input_files]
            for net_test in provided_net_tests:
                if net_test['name'] != test_name:
                    continue
                if net_test['version'] != test_version:
                    continue
                if set(net_test['input-hashes']) != set(input_files):
                    continue
                return net_test['collector'], net_test['test-helpers']

        for net_test_loader in self.netTestLoaders:
            log.msg("Setting collector and test helpers for %s" %
                    net_test_loader.testName)

            collector, test_helpers = \
                find_collector_and_test_helpers(net_test_loader.testName,
                                                net_test_loader.testVersion,
                                                net_test_loader.inputFiles)

            for option, name in net_test_loader.missingTestHelpers:
                test_helper_address = test_helpers[name].encode('utf-8')
                net_test_loader.localOptions[option] = test_helper_address
                net_test_loader.testHelpers[option] = test_helper_address

            if not net_test_loader.collector:
                net_test_loader.collector = collector.encode('utf-8')

    @defer.inlineCallbacks
    def lookupTestHelpers(self):
        self.oonibclient.address = self.bouncer

        required_test_helpers = []
        requires_collector = []
        for net_test_loader in self.netTestLoaders:
            if not net_test_loader.collector and not self.no_collector:
                requires_collector.append(net_test_loader)

            required_test_helpers += map(lambda x: x[1],
                                           net_test_loader.missingTestHelpers)

        if not required_test_helpers and not requires_collector:
            defer.returnValue(None)

        response = yield self.oonibclient.lookupTestHelpers(required_test_helpers)

        for net_test_loader in self.netTestLoaders:
            log.msg("Setting collector and test helpers for %s" %
                    net_test_loader.testName)

            # Only set the collector if the no collector has been specified
            # from the command line or via the test deck.
            if len(net_test_loader.missingTestHelpers) == 0 and \
                            net_test_loader in requires_collector:
                log.msg("Using the default collector: %s" %
                        response['default']['collector'])
                net_test_loader.collector = response['default']['collector'].encode('utf-8')
                continue

            for option, name in net_test_loader.missingTestHelpers:
                test_helper_address = response[name]['address'].encode('utf-8')
                test_helper_collector = \
                    response[name]['collector'].encode('utf-8')

                log.msg("Using this helper: %s" % test_helper_address)
                net_test_loader.localOptions[option] = test_helper_address
                net_test_loader.testHelpers[option] = test_helper_address
                if net_test_loader in requires_collector:
                    net_test_loader.collector = test_helper_collector

    @defer.inlineCallbacks
    def fetchAndVerifyNetTestInput(self, net_test_loader):
        """ fetch and verify a single NetTest's inputs """
        log.debug("Fetching and verifying inputs")
        for i in net_test_loader.inputFiles:
            if i['url']:
                log.debug("Downloading %s" % i['url'])
                self.oonibclient.address = i['address']

                try:
                    input_file = yield self.oonibclient.downloadInput(i['hash'])
                except:
                    raise e.UnableToLoadDeckInput

                try:
                    input_file.verify()
                except AssertionError:
                    raise e.UnableToLoadDeckInput

                i['test_options'][i['key']] = input_file.cached_file
