#!/usr/bin/env python

import sys, os, string, socket

def createSocket():
    servAddr = os.getenv("TEXTTEST_MIM_SERVER")
    if servAddr:
        host, port = servAddr.split(":")
        serverAddress = (host, int(port))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(serverAddress)
        return sock

def sendServerState(stateDesc):
    sock = createSocket()
    if sock:
        sock.sendall("SUT_SERVER:" + stateDesc + os.linesep)
        sock.close()

if __name__ == "__main__":
    sock = createSocket()
    text = "SUT_COMMAND_LINE:" + repr(sys.argv) + ":SUT_ENVIRONMENT:" + repr(os.environ)
    sock.sendall(text)
    sock.shutdown(1)
    response = sock.recv(1000000, socket.MSG_WAITALL)
    sock.close()
    try:
        stdout, stderr = response.split("|TT_STDOUT_STDERR|")
        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
    except ValueError:
        sys.stderr.write("Received unexpected communication from MIM server: " + response)

