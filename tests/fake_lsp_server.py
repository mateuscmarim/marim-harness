"""A minimal LSP server over stdio for tests: answers initialize, acks
initialized/shutdown, and returns one definition location. Speaks the
Content-Length framing multilspy's client uses. No third-party deps."""
import json
import sys


def _read_message(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode("utf-8").rstrip("\r\n")
        if line == "":
            break
        key, _, val = line.partition(":")
        headers[key.strip().lower()] = val.strip()
    length = int(headers.get("content-length", "0"))
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))


def _write_message(stream, payload):
    data = json.dumps(payload).encode("utf-8")
    stream.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
    stream.write(data)
    stream.flush()


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        msg = _read_message(stdin)
        if msg is None:
            return
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            _write_message(stdout, {
                "jsonrpc": "2.0", "id": mid,
                "result": {"capabilities": {"definitionProvider": True,
                                            "referencesProvider": True,
                                            "documentSymbolProvider": True}},
            })
        elif method == "textDocument/definition":
            _write_message(stdout, {
                "jsonrpc": "2.0", "id": mid,
                "result": [{"uri": msg["params"]["textDocument"]["uri"],
                            "range": {"start": {"line": 0, "character": 0},
                                      "end": {"line": 0, "character": 1}}}],
            })
        elif method == "shutdown":
            _write_message(stdout, {"jsonrpc": "2.0", "id": mid, "result": None})
        elif method == "exit":
            return
        # notifications (initialized, didOpen, ...) need no reply


if __name__ == "__main__":
    main()
