#!/usr/bin/env python3
"Reduce a Web Services Description down to a single file"

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urljoin
import argparse
import gzip
import importlib
import logging
import re
import os
import site
import sys

from lxml import etree
from requests import Session
from requests.exceptions import HTTPError

if __name__ == "__main__":
    sys.path.insert(0, Path(__file__).parent.parent)
    importlib.reload(site)

from wsdl2file.clark import clark, declark
import wsdl2file.zeep as z

LOGGER = logging.getLogger()
LOG_FORMAT = "%(asctime)s [%(process)d] [%(levelname)s] [%(name)s] %(message)s"


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_arguments()

    def add_arguments(self):
        self.add_argument("url", type=str, help="location of the WSDL")
        self.add_argument(
            "--log-level",
            type=str,
            default=os.environ.get("LOG_LEVEL", "info"),
            help="log level")
        self.add_argument(
            "--client-cert", type=str, default=None,
            help="Client certificate and key as one .PEM file")
        # for debugging -- keep attributes in Clark Notation instead of
        # re-namespacing them
        self.add_argument(
            "--keep-clark", action='store_true', help=argparse.SUPPRESS)
        self.add_argument(
            "--zeep", action='store_true',
            help="Attempt to make compatible with zeep")

    def parse_args(self, *args, **kwargs):
        options = super().parse_args(*args, **kwargs)
        options.log_level = options.log_level.upper()
        url = urlparse(options.url)
        if not url.scheme:
            options.url = url._replace(scheme="file").geturl()
        return options


class DocumentLoader:
    def __init__(self, session: Session | None=None):
        self.session: Session = session or Session()
        self.seen: set = set()

    def load_xml(self, url: str, always=False):
        """Load document `url`, unless we already have.
        Returns a tuple of (document, url), or (None, None) if the document
        has already been loaded.
        `url` may differ from the original URL if a redirect was followed.
        If `always` is True, always load the document even if already loaded
        """
        if url in self.seen and not always:
            LOGGER.debug("Already loaded %s", url)
            return None, None
        LOGGER.info("Loading %s", url)
        response = self.session.get(url, stream=True, allow_redirects=True)
        response.raise_for_status()
        if response.headers.get('content-encoding', '') == 'gzip':
            fh = gzip.open(response.raw)
        else:
            fh = response.raw
        document = etree.parse(fh)
        self.seen.add(url)
        self.seen.add(response.url)
        return document, response.url

class ClarkDocumentLoader(DocumentLoader):
    """DocumentLoader that converts certain attributes of certain tags
    to Clark Notation.

    WSDL Schemas are namespaced. The namespace prefixes may differ between
    differnent included files. For example, some files reference
    http://www.w3.org/2001/XMLSchema as "xs" whereas other reference as "xsd".
    ElementTree will transparently standardize the prefixes for *tags* across
    these documents when they are joined together, however, it has no way to
    know which *attributes* refer to XML-namespaced items.

    This walks a document after loading, converting the named attributes to
    Clark Notation.
    """
    def __init__(self, attribute_map = None, session: Session | None = None):
        """
        attribute_map - keys are fully-qualified tag names in clark notation.
                        values are lists of attribute names
        """
        self.attribute_map = attribute_map
        super().__init__()


    def load_xml(self, *args, **kwargs):
        document, url = super().load_xml(*args, **kwargs)
        if document:
            clark(document.getroot(), attribute_map=self.attribute_map)
        return document, url


def element_url(ele):
    url = ele.attrib.get('location')
    if not url:
        url = ele.attrib.get('schemaLocation')
    if not url:
        return
    return url


def get_references(document):
    "Return any WSDL include tags found in `document`"
    tree = document.getroot()
    references = []
    wsdl = tree.find('{http://schemas.xmlsoap.org/wsdl/}import')
    if wsdl is not None:
        references.append(wsdl)
    if tree.tag == '{http://www.w3.org/2001/XMLSchema}schema':
        schemas = [tree]
    else:
        schemas = tree.findall('.//{http://www.w3.org/2001/XMLSchema}schema')
    for schema in schemas:
        imports = schema.findall('{http://www.w3.org/2001/XMLSchema}import')
        references += imports
    for schema in schemas:
        includes = schema.findall('{http://www.w3.org/2001/XMLSchema}include')
        references += includes
    return references


def url2abs(url, base_url):
    "Make possibly-relative `url` absolute to `base_url`"
    # Microsoft WCF likes to put backslashes in some URLs instead of slashes
    url = re.sub(r'\\', '/', url)
    url = re.sub(r'%5[cC]', '/', url)
    url = urljoin(base_url, url)
    return url


def fix_references(references, base_url):
    "Make any relative references in `references` absolute"
    LOGGER.debug("Fixing references for %s", base_url)
    for ele in references:
        for attr in {'location', 'schemaLocation'}:
            url = ele.attrib.get(attr)
            if url is not None:
                new_url = url2abs(url, base_url)
                if url != new_url:
                    LOGGER.debug("%s: %s -> %s", base_url, url, new_url)
                    ele.set(attr, new_url)

def include_url(ele):
    "Return the URL that XML tag `include` references"
    url = ele.attrib.get('location')
    if not url:
        url = ele.attrib.get('schemaLocation')
    if not url:
        return
    return url

def inline_next_xsd(loader, doc, uri):
    """
    Import or include the next XSD file directly into the document, if needed.

    If the next XSD file has already been imported, simply deletes
    the "schemaLocation" tag from that import so that it can be
    referenced directly in the file.

    The new XSD is inserted before the XSD that references it, as it is
    a dependency that must be parsed first.

    Returns a tuple. The first value indicates if a tag was modified,
    the second value indicates if a document was imported or included.
    """
    schemas = doc.getroot().findall(
        ".//{http://www.w3.org/2001/XMLSchema}schema")
    for schema in schemas:
        # Process the next import, removing the "schemaLocation" attribute.
        imports = schema.findall("{http://www.w3.org/2001/XMLSchema}import")
        for imp in imports:
            imp_url = imp.attrib.pop("schemaLocation", None)
            if imp_url is None:
                continue
            imp_doc, imp_url_out = loader.load_xml(imp_url)
            if imp_doc is None:
                return True, False
            references = get_references(imp_doc)
            fix_references(references, imp_url_out)
            tag_namespace = imp.attrib.get(
                "namespace",
                imp.getparent().attrib.get("targetNamespace", None))
            imp_root = imp_doc.getroot()
            imp_namespace = imp_root.attrib.get("targetNamespace", None)
            schema.addprevious(imp_doc.getroot())
            return True, True

        # Process the next include, removing the include tag and replacing
        # it with the XSD content
        includes = schema.findall("{http://www.w3.org/2001/XMLSchema}include")
        for inc in includes:
            inc_url = inc.attrib.get("schemaLocation", None)
            if inc_url is None:
                LOGGER.warning("found an include tag without a location")
                inc.addprevious(
                    etree.Comment("found an include tag without a location"))
                inc.getparent().replace(inc, etree.Comment(etree.tostring(inc)))
                return True, False
            inc_doc, inc_url_out = loader.load_xml(inc_url)
            if inc_doc is None:
                inc.addprevious(etree.Comment("include was already loaded"))
                inc.getparent().replace(inc, etree.Comment(etree.tostring(inc)))
                return True, False
            references = get_references(inc_doc)
            fix_references(references, inc_url_out)
            children = inc_doc.getroot().getchildren()
            if children is not None:
                for element in reversed(children):
                    inc.addnext(element)
            inc.getparent().replace(inc, etree.Comment(etree.tostring(inc)))
            return True, True
    return False, False


def inline_xsd_references(loader, doc, url):
    """Attempt to import all XSD files directly into the document

    Return value is a tuple - the first value is the number of XSD import
    tags modified, and the second is the number of unique files actually
    imported."""
    run = True
    modified, imported = 0, 0
    while run:
        run, new = inline_next_xsd(loader, doc, url)
        if run:
            modified += 1
        if new:
            imported += 1
    LOGGER.info(
        "%s: imported %u XSD files, modified %u XSD import/include tags",
        url, imported, modified)
    return modified, imported


def wsdl2dom(url: str, client_cert=None, keep_clark=False, zeep=False):
    "Convert the WSDL at `url` to a single DOM tree"
    session = Session()
    if client_cert:
        session.cert = client_cert
    loader = ClarkDocumentLoader()
    doc, url_out = loader.load_xml(url)
    references = get_references(doc)
    fix_references(references, url_out)
    inline_xsd_references(loader, doc, url_out)
    if zeep:
        elements = z.replace_refs(doc.getroot())
        LOGGER.info("Made %u elements compatible with Zeep", elements)      
    if not keep_clark:
        declark(doc.getroot())
    return doc


def main(args=None):
    args = args or sys.argv[1:]
    parser = ArgumentParser()
    options = parser.parse_args(args)
    options.log_level = options.log_level.upper()
    logging.basicConfig(format=LOG_FORMAT, level=options.log_level)
    doc = wsdl2dom(
        options.url,
        client_cert=options.client_cert,
        keep_clark=options.keep_clark,
        zeep=options.zeep)
    etree.indent(doc, space=' ', level=2)
    print(etree.tostring(doc.getroot()).decode())
    return(0)


if __name__ == "__main__":
    exit(main())
