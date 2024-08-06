#!/usr/bin/env python3
"""
Attempt to make a WSDL compatible with Zeep

This package does known-safe transformations on an WSDL document in an attempt
to make it compatible with Zeep. This is neccessary because Zeep does noit
support SubstitutionGroups
(https://github.com/mvantellingen/python-zeep/issues/321).

The WSDL's tags must have been converted from namespace prefixes to
Clark Notation.
"""

import logging

from pathlib import Path
from lxml import etree

LOGGER = logging.getLogger(__name__)

XSD = "http://www.w3.org/2001/XMLSchema"
NC = "http://release.niem.gov/niem/niem-core/4.0/"
# list of lists, each list [selector -> new_ref_attribute]
REF_TRANSFORMATIONS = [
    [
        f".//{{{XSD}}}complexType[@name='DateType']//{{{XSD}}}element[@ref='{{{NC}}}DateRepresentation']",
        f"{{{NC}}}DateTime"
    ]
]

def replace_ref(tree, path: str, new_ref: str) -> int:
    n = 0
    for node in tree.findall(path):
        node.attrib["ref"] = new_ref
        n += 1
    LOGGER.debug("%s -> %s: %u", path, new_ref, n)
    return n

def replace_refs(tree, transformations: list=REF_TRANSFORMATIONS) -> int:
    n = 0
    for transformation in transformations:
        n += replace_ref(tree, *transformation)
    return n