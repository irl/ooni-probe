import os
import json

from urlparse import urljoin, urlparse

from twisted.web.error import Error
from twisted.web.client import Agent, Headers
from twisted.internet import defer, reactor
from twisted.internet.endpoints import TCP4ClientEndpoint

from twisted.python.versions import Version
from twisted import version as _twisted_version
_twisted_14_0_2_version = Version('twisted', 14, 0, 2)

from ooni import errors as e
from ooni.settings import config
from ooni.utils import log, onion
from ooni.utils.net import BodyReceiver, StringProducer, Downloader
from ooni.utils.socks import TrueHeadersSOCKS5Agent


class OONIBClient(object):
    def __init__(self, address=None, settings={}):
        self.base_headers = {}
        self.backend_type = settings.get('type', None)
        self.base_address = settings.get('address', address)

        if self.backend_type is None:
            self._guessBackendType()
        self.backend_type = self.backend_type.encode('ascii')

        if self.backend_type == 'cloudfront':
            self.base_headers['Host'] = settings['front'].encode('ascii')

        self._setupBaseAddress()
        self.settings = {
            'type': self.backend_type,
            'address': self.base_address,
            'front': settings.get('front', '').encode('ascii')
        }

    def _guessBackendType(self):
        if self.base_address is None:
            raise e.InvalidAddress
        if onion.is_onion_address(self.base_address):
            self.backend_type = 'onion'
        elif self.base_address.startswith('https://'):
            self.backend_type = 'https'
        elif self.base_address.startswith('http://'):
            self.backend_type = 'http'
        else:
            raise e.InvalidAddress

    def _setupBaseAddress(self):
        parsed_address = urlparse(self.base_address)
        if self.backend_type == 'onion':
            if not onion.is_onion_address(self.base_address):
                log.err("Invalid onion address.")
                raise e.InvalidAddress(self.base_address)
            if parsed_address.scheme in ('http', 'httpo'):
                self.base_address = ("http://%s" % parsed_address.netloc)
            else:
                self.base_address = ("%s://%s" % (parsed_address.scheme,
                                                  parsed_address.netloc))
        elif self.backend_type == 'http':
            self.base_address = ("http://%s" % parsed_address.netloc)
        elif self.backend_type in ('https', 'cloudfront'):
            self.base_address = ("https://%s" % parsed_address.netloc)
        self.base_address = self.base_address.encode('ascii')

    def isSupported(self):
        if self.backend_type in ("https", "cloudfront"):
            if _twisted_version < _twisted_14_0_2_version:
                log.err("HTTPS and cloudfronted backends require "
                        "twisted > 14.0.2.")
                return False
        elif self.backend_type == "http":
            if config.advanced.insecure_backend is not True:
                log.err("Plaintext backends are not supported. To "
                        "enable at your own risk set "
                        "advanced->insecure_backend to true")
                return False
        elif self.backend_type == "onion":
            # XXX add an extra check to ensure tor is running
            if not config.tor_state and config.tor.socks_port is None:
                return False
        return True

    def isReachable(self):
        raise NotImplemented

    def _request(self, method, urn, genReceiver, bodyProducer=None, retries=3):
        if self.backend_type == 'onion':
            agent = TrueHeadersSOCKS5Agent(reactor,
                                           proxyEndpoint=TCP4ClientEndpoint(reactor,
                                                                            '127.0.0.1',
                                                                            config.tor.socks_port))
        else:
            agent = Agent(reactor)

        attempts = 0

        finished = defer.Deferred()

        def perform_request(attempts):
            uri = urljoin(self.base_address, urn)
            d = agent.request(method, uri, bodyProducer=bodyProducer,
                              headers=Headers(self.base_headers))

            @d.addCallback
            def callback(response):
                try:
                    content_length = int(response.headers.getRawHeaders('content-length')[0])
                except:
                    content_length = None
                response.deliverBody(genReceiver(finished, content_length))

            def errback(err, attempts):
                # We we will recursively keep trying to perform a request until
                # we have reached the retry count.
                if attempts < retries:
                    log.err("Lookup failed. Retrying.")
                    attempts += 1
                    perform_request(attempts)
                else:
                    log.err("Failed. Giving up.")
                    finished.errback(err)

            d.addErrback(errback, attempts)

        perform_request(attempts)

        return finished

    def queryBackend(self, method, urn, query=None, retries=3):
        bodyProducer = None
        if query:
            bodyProducer = StringProducer(json.dumps(query))

        def genReceiver(finished, content_length):
            def process_response(s):
                # If empty string then don't parse it.
                if not s:
                    return
                try:
                    response = json.loads(s)
                except ValueError:
                    raise e.get_error(None)
                if 'error' in response:
                    log.debug("Got this backend error message %s" % response)
                    raise e.get_error(response['error'])
                return response

            return BodyReceiver(finished, content_length, process_response)

        return self._request(method, urn, genReceiver, bodyProducer, retries)

    def download(self, urn, download_path):

        def genReceiver(finished, content_length):
            return Downloader(download_path, finished, content_length)

        return self._request('GET', urn, genReceiver)

class BouncerClient(OONIBClient):
    def isReachable(self):
        return defer.succeed(True)

    @defer.inlineCallbacks
    def lookupTestCollector(self, net_tests):
        try:
            test_collector = yield self.queryBackend('POST', '/bouncer/net-tests',
                                                     query={'net-tests': net_tests})
        except Exception as exc:
            log.exception(exc)
            raise e.CouldNotFindTestCollector

        defer.returnValue(test_collector)

    @defer.inlineCallbacks
    def lookupTestHelpers(self, test_helper_names):
        try:
            test_helper = yield self.queryBackend('POST', '/bouncer/test-helpers',
                                                  query={'test-helpers': test_helper_names})
        except Exception as exc:
            log.exception(exc)
            raise e.CouldNotFindTestHelper

        if not test_helper:
            raise e.CouldNotFindTestHelper

        defer.returnValue(test_helper)


class CollectorClient(OONIBClient):
    def isReachable(self):
        # XXX maybe in the future we can have a dedicated API endpoint to
        # test the reachability of the collector.
        d = self.queryBackend('GET', '/invalidpath')

        @d.addCallback
        def cb(_):
            # We should never be getting an acceptable response for a
            # request to an invalid path.
            return False

        @d.addErrback
        def err(failure):
            failure.trap(Error)
            return failure.value.status == '404'

        return d

    def getInput(self, input_hash):
        from ooni.deck import InputFile

        input_file = InputFile(input_hash)
        if input_file.descriptorCached:
            return defer.succeed(input_file)
        else:
            d = self.queryBackend('GET', '/input/' + input_hash)

            @d.addCallback
            def cb(descriptor):
                input_file.load(descriptor)
                input_file.save()
                return input_file

            @d.addErrback
            def err(err):
                log.err("Failed to get descriptor for input %s" % input_hash)
                log.exception(err)

            return d

    def getInputList(self):
        return self.queryBackend('GET', '/input')

    def downloadInput(self, input_hash):
        from ooni.deck import InputFile

        input_file = InputFile(input_hash)

        if input_file.fileCached:
            return defer.succeed(input_file)
        else:
            d = self.download('/input/' + input_hash + '/file', input_file.cached_file)

            @d.addCallback
            def cb(res):
                input_file.verify()
                return input_file

            @d.addErrback
            def err(err):
                log.err("Failed to download the input file %s" % input_hash)
                log.exception(err)

            return d

    def getInputPolicy(self):
        return self.queryBackend('GET', '/policy/input')

    def getNettestPolicy(self):
        return self.queryBackend('GET', '/policy/nettest')

    def getDeckList(self):
        return self.queryBackend('GET', '/deck')

    def getDeck(self, deck_hash):
        from ooni.deck import Deck

        deck = Deck(deck_hash)
        if deck.descriptorCached:
            return defer.succeed(deck)
        else:
            d = self.queryBackend('GET', '/deck/' + deck_hash)

            @d.addCallback
            def cb(descriptor):
                deck.load(descriptor)
                deck.save()
                return deck

            @d.addErrback
            def err(err):
                log.err("Failed to get descriptor for deck %s" % deck_hash)
                log.exception(err)

            return d

    def downloadDeck(self, deck_hash):
        from ooni.deck import Deck

        deck = Deck(deck_hash)
        if deck.fileCached:
            return defer.succeed(deck)
        else:
            d = self.download('/deck/' + deck_hash + '/file', deck.cached_file)

            @d.addCallback
            def cb(res):
                deck.verify()
                return deck

            @d.addErrback
            def err(err):
                log.err("Failed to download the deck %s" % deck_hash)
                log.exception(err)

            return d

    def createReport(self, test_details):
        request = {
            'software_name': test_details['software_name'],
            'software_version': test_details['software_version'],
            'probe_asn': test_details['probe_asn'],
            'probe_cc': test_details['probe_cc'],
            'test_name': test_details['test_name'],
            'test_version': test_details['test_version'],
            'test_start_time': test_details['test_start_time'],
            'input_hashes': test_details['input_hashes'],
            'data_format_version': test_details['data_format_version'],
            'format': 'json'
        }
        # import values from the environment
        request.update([(k.lower(),v) for (k,v) in os.environ.iteritems()
                        if k.startswith('PROBE_')])

        return self.queryBackend('POST', '/report', query=request)

    def updateReport(self, report_id, serialization_format, entry_content):
        request = {
            'format': serialization_format,
            'content': entry_content
        }
        return self.queryBackend('POST', '/report/%s' % report_id,
                                 query=request)


    def closeReport(self, report_id):
        return self.queryBackend('POST', '/report/' + report_id + '/close')

class WebConnectivityClient(OONIBClient):
    def isReachable(self):
        d = self.queryBackend('GET', '/status')

        @d.addCallback
        def cb(result):
            if result.get("status", None) != "ok":
                return False
            return True

        @d.addErrback
        def err(_):
            return False

        return d

    def control(self, http_request, tcp_connect):
        request = {
            'http_request': http_request,
            'tcp_connect': tcp_connect
        }
        return self.queryBackend('POST', '/', query=request)
