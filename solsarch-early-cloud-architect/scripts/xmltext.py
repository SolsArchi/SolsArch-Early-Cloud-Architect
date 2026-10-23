#!/usr/bin/env python3
"""Escape text for XML and SVG output (the three characters that matter, plus any extra entities the caller names).

Written here so the tool does not import the xml parser packages, which static scanners flag even when only this one function is used."""


def escape(text, entities=None):
    out = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char, entity in (entities or {}).items():
        out = out.replace(char, entity)
    return out
