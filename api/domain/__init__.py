"""Pure domain of the replacement.

It knows no HTTP, knows no SQL and performs no I/O: it receives the message and
a state port and returns a typed result. The status and JSON keys live in the
serializer, not here.
"""
