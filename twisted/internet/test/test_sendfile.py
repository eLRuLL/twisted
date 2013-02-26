# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for sendfile.
"""

import socket

from twisted.trial.unittest import TestCase, SkipTest
from twisted.internet.defer import Deferred
from twisted.internet._sendfile import SendfileInfo
from twisted.internet import error, _sendfile
from twisted.internet.interfaces import IReactorFDSet
from twisted.python import log
from twisted.python.filepath import FilePath
from twisted.python.deprecate import _fullyQualifiedName as fullyQualifiedName

from twisted.test.proto_helpers import AccumulatingProtocol

if _sendfile.sendfile is None:
    sendfileSkip = "sendfile not available"
else:
    sendfileSkip = None



class SendfileClientProtocol(AccumulatingProtocol):
    """
    Protocol used on the client receiving data of the sendfile server.

    @ivar doneDeferred: deferred fired when all expected data has been
        received.
    @ivar expected: amount of data expected.
    """
    doneDeferred = None
    expected = 0

    def dataReceived(self, data):
        """
        Store the data, and fire the deferred if all data has been received.
        """
        self.data += data
        if len(self.data) >= self.expected and self.doneDeferred:
            doneDeferred, self.doneDeferred = self.doneDeferred, None
            doneDeferred.callback(self.data)


    def connectionLost(self, reason):
        """
        On connection lost, forward to C{reason} to L{AccumulatingProtocol},
        and fires C{doneDeferred} if it's still present.
        """
        AccumulatingProtocol.connectionLost(self, reason)
        if self.doneDeferred:
            doneDeferred, self.doneDeferred = self.doneDeferred, None
            doneDeferred.errback(reason)



class SendfileIntegrationMixin(object):
    """
    Tests for
    L{writeFile<twisted.internet.interfaces.IWriteFileTransport.writeFile>}.
    """

    def createFile(self):
        """
        Create a file to send during tests.

        @return: A file opened ready to be sent.
        """
        path = FilePath(self.mktemp())
        path.setContent(b'x' * 1000000)
        f = path.open()
        self.addCleanup(f.close)
        return f


    def test_writeFileServer(self):
        """
        C{IWriteFileTransport.writeFile} sends the whole content of a file over
        the wire.
        """

        def connected(protocols):
            client, server = protocols[:2]
            client.doneDeferred = doneDeferred = Deferred()
            client.expected = 1000000
            fileObject = self.createFile()
            server.transport.writeFile(fileObject)
            doneDeferred.addCallback(finished, client)
            doneDeferred.addBoth(stop, client)

        def finished(data, client):
            self.assertEqual(1000000, len(data))

        def stop(passthrough, client):
            client.transport.loseConnection()
            return passthrough

        reactor = self.buildReactor()
        d = self.getConnectedClientAndServer(
            reactor, "127.0.0.1", socket.AF_INET, SendfileClientProtocol)
        d.addCallback(connected)
        d.addErrback(log.err)
        self.runReactor(reactor)


    def test_writeFileClient(self):
        """
        C{IWriteFileTransport.writeFile} works from client transport side as
        well.
        """

        def connected(protocols):
            client, server = protocols[:2]
            server.doneDeferred = doneDeferred = Deferred()
            server.expected = 1000000
            fileObject = self.createFile()
            client.transport.writeFile(fileObject)
            doneDeferred.addCallback(finished, server)
            doneDeferred.addBoth(stop, server)

        def finished(data, server):
            self.assertEqual(1000000, len(data))

        def stop(passthrough, server):
            server.transport.loseConnection()
            return passthrough

        reactor = self.buildReactor()
        d = self.getConnectedClientAndServer(
            reactor, "127.0.0.1", socket.AF_INET,
            protocolServerFactory=SendfileClientProtocol)
        d.addCallback(connected)
        d.addErrback(log.err)
        self.runReactor(reactor)


    def test_writeFilePendingData(self):
        """
        C{IWriteFileTransport.writeFile} doesn't take precedence over previous
        C{ITCPTransport.write} calls, the data staying in the same order.
        """

        def connected(protocols):
            client, server = protocols[:2]
            client.doneDeferred = doneDeferred = Deferred()
            client.expected = 1000010
            server.transport.write(b'y' * 10)
            doneDeferred.addCallback(finished, client)
            doneDeferred.addBoth(stop, client)
            fileObject = self.createFile()
            return server.transport.writeFile(fileObject)

        def finished(data, client):
            self.assertEqual(1000010, len(data))
            self.assertEqual(b'y' * 10, data[:10])
            self.assertEqual(b'x' * 10, data[10:20])

        def stop(passthrough, client):
            client.transport.loseConnection()
            return passthrough

        reactor = self.buildReactor()
        d = self.getConnectedClientAndServer(
            reactor, "127.0.0.1", socket.AF_INET, SendfileClientProtocol)
        d.addCallback(connected)
        d.addErrback(log.err)
        self.runReactor(reactor)


    def test_writeFileStopWriting(self):
        """
        At the end of a successfull C{IWriteFileTransport.writeFile} call, the
        transport is unregistered from the reactor as there is no pending data
        to send.
        """
        reactor = self.buildReactor()
        if not IReactorFDSet.providedBy(reactor):
            raise SkipTest("%s does not provide IReactorFDSet" % (
                fullyQualifiedName(reactor.__class__),))

        def connected(protocols):
            client, server = protocols[:2]
            client.doneDeferred = doneDeferred = Deferred()
            client.expected = 1000001
            fileObject = self.createFile()
            writeDeferred = server.transport.writeFile(fileObject)
            writeDeferred.addCallback(checkServer, server)
            doneDeferred.addCallback(finished, client)
            doneDeferred.addBoth(stop, client)

        def checkServer(ign, server):
            reactor.callLater(0, checkWriter, server)

        def checkWriter(server):
            self.assertNotIn(server.transport, reactor.getWriters())
            server.transport.write(b"x")

        def finished(data, client):
            self.assertEqual(1000001, len(data))

        def stop(passthrough, client):
            client.transport.loseConnection()
            return passthrough

        d = self.getConnectedClientAndServer(
            reactor, "127.0.0.1", socket.AF_INET, SendfileClientProtocol)
        d.addCallback(connected)
        d.addErrback(log.err)
        self.runReactor(reactor)


    def test_writeFileError(self):
        """
        When an error happens during the C{IWriteFileTransport.writeFile} call,
        the L{Deferred} returned is fired with that error. We simulate that
        case by patching C{sendfile} after some data, and make sure that we get
        the error and that transfer is interrupted.
        """

        def sendError(*args):
            raise IOError("That's bad!")

        def connected(protocols):
            client, server = protocols[:2]
            client.closedDeferred.addCallback(clientFinished, client)
            client.doneDeferred = doneDeferred = Deferred()
            client.expected = 100000
            doneDeferred.addCallback(dataFinished, client)
            fileObject = self.createFile()
            sendFileDeferred = server.transport.writeFile(fileObject)
            return sendFileDeferred.addErrback(serverFinished)

        def dataFinished(data, client):
            self.assertTrue(len(data) >= 100000)
            self.patch(_sendfile, "sendfile", sendError)

        def clientFinished(ignored, client):
            self.assertIsInstance(client.closedReason.value,
                                  error.ConnectionDone)
            self.assertTrue(len(client.data) < 1000000)

        def serverFinished(error):
            error.trap(IOError)

        reactor = self.buildReactor()
        d = self.getConnectedClientAndServer(
            reactor, "127.0.0.1", socket.AF_INET, SendfileClientProtocol)
        d.addCallback(connected)
        d.addErrback(log.err)
        self.runReactor(reactor)

    if sendfileSkip:
        test_writeFileError.skip = sendfileSkip


    def test_writeFileErrorAtFirstSight(self):
        """
        If the C{sendfile} system calls fails at the start of the transfer with
        an C{IOError}, C{IWriteFileTransport.writeFile} falls back to a
        standard producer.
        """

        def sendError(*args):
            raise IOError("That's bad!")

        self.patch(_sendfile, "sendfile", sendError)

        def connected(protocols):
            client, server = protocols[:2]
            client.doneDeferred = doneDeferred = Deferred()
            client.expected = 1000000
            fileObject = self.createFile()
            server.transport.writeFile(fileObject)
            doneDeferred.addCallback(finished, client)
            doneDeferred.addBoth(stop, client)

        def finished(data, client):
            self.assertEqual(1000000, len(data))

        def stop(passthrough, client):
            client.transport.loseConnection()
            return passthrough

        reactor = self.buildReactor()
        d = self.getConnectedClientAndServer(
            reactor, "127.0.0.1", socket.AF_INET, SendfileClientProtocol)
        d.addCallback(connected)
        d.addErrback(log.err)
        self.runReactor(reactor)

    if sendfileSkip:
        test_writeFileErrorAtFirstSight.skip = sendfileSkip



class SendfileInfoTestCase(TestCase):
    """
    Tests for L{SendfileInfo}.
    """

    def test_lengthDetection(self):
        """
        L{SendfileInfo} is able to detect the file length and preserves the
        file position when doing so.
        """
        path = FilePath(self.mktemp())
        path.setContent(b'x' * 42)
        f = path.open()
        f.seek(7)
        self.addCleanup(f.close)
        sfi = SendfileInfo(f)
        self.assertEqual(sfi.count, 42)
        self.assertEqual(f.tell(), 7)
