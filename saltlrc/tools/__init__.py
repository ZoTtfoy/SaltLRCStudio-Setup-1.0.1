"""Domain-free utilities: things that know nothing about lyrics.

Kept apart from the layers on purpose. A QR encoder has no business sitting
next to the document model, and mixing them is how "which import is the real
one?" questions start.
"""
