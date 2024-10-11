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
from requests.exceptions import HTTPError
from requests_file import FileAdapter
import requests

if __name__ == "__main__":
    sys.path.insert(0, Path(__file__).parent.parent)
    importlib.reload(site)

from wsdl2file.clark import clark, declark
from wsdl2file.const import WSDL_NS, XSD_NS


LOGGER = logging.getLogger()
LOG_FORMAT = "%(asctime)s [%(process)d] [%(levelname)s] [%(name)s] %(message)s"


class Session(requests.Session):
    def __init__(self, *args, cert=None, **kwargs):
        super().__init__(*args, **kwargs)
        if cert:
            self.cert = cert
        self.mount("file://", FileAdapter())


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

    def parse_args(self, *args, **kwargs):
        options = super().parse_args(*args, **kwargs)
        options.log_level = options.log_level.upper()
        url = urlparse(options.url)
        if not url.scheme:
            url = urlparse(str(Path(options.url).resolve()))
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
        super().__init__(session=session)


    def load_xml(self, *args, **kwargs):
        document, url = super().load_xml(*args, **kwargs)
        if document:
            clark(document.getroot(), attribute_map=self.attribute_map)
        return document, url


def get_references(document):
    "Return any WSDL include tags found in `document`"
    tree = document.getroot()
    references = []
    wsdls = (
        tree.findall(f'{{{WSDL_NS}}}import') +
        tree.findall(f'{{{WSDL_NS}}}include'))
    for wsdl in wsdls:
        references.append(wsdl)
    if tree.tag == f'{{{XSD_NS}}}schema':
        schemas = [tree]
    else:
        schemas = tree.findall(f'.//{{{XSD_NS}}}schema')
    for schema in schemas:
        imports = schema.findall(f'{{{XSD_NS}}}import')
        references += imports
    for schema in schemas:
        includes = schema.findall(f'{{{XSD_NS}}}include')
        references += includes
    return references


def url2abs(url, base_url):
    "Make possibly-relative `url` absolute to `base_url`"
    # Microsoft WCF likes to put backslashes in some URLs instead of slashes
    url = re.sub(r'\\', '/', url)
    url = re.sub(r'%5[cC]', '/', url)
    url = urljoin(base_url, url)
    return url


def fix_references(doc, references, base_url):
    """
    1. Make any relative references in `references` absolute
    2. Change <wsdl:import>s that import to XSD schemas
    """
    LOGGER.debug("Fixing references for %s", base_url)
    types_tag = doc.find(f"./{{{WSDL_NS}}}types")
    xsd_fixed = 0
    for ele in references:
        for attr in {'location', 'schemaLocation'}:
            url = ele.attrib.get(attr)
            if url is not None:
                new_url = url2abs(url, base_url)
                if url != new_url:
                    LOGGER.debug("%s: %s -> %s", base_url, url, new_url)
                    ele.set(attr, new_url)
        loc = ele.attrib.get("location", "")
        if ele.tag == f"{{{WSDL_NS}}}import" and loc.endswith(".xsd"):
            schema_tag = etree.Element(f"{{{XSD_NS}}}schema")
            import_tag = etree.Element(f"{{{XSD_NS}}}import")
            import_tag.attrib["namespace"] = ele.attrib["namespace"]
            import_tag.attrib["schemaLocation"] = ele.attrib["location"]
            schema_tag.append(import_tag)
            if types_tag is None:
                types_tag = etree.Element(f"{{{WSDL_NS}}}types")
                doc.getroot().insert(0, types_tag)
            types_tag.insert(xsd_fixed, schema_tag)
            xsd_fixed += 1
            ele.getparent().remove(ele)
            LOGGER.debug("%s: moved from WSDL import to XS import", loc)

def merge_root_nodes(
        main_doc: etree._Document, incoming_doc: etree._Document
) -> etree._Element:
    """Create a new document root tag that has the namespaces of both documents,
    preferring the main document's nsmap. This is necessary because while
    lxml will only copy namespaces over if they are used on tag names or
    attribute names, and we also need to make sure that all namespace prefixes
    for attribute *values* are copied over.
    """
    main_root, incoming_root = main_doc.getroot(), incoming_doc.getroot()
    if main_root.tag != incoming_root.tag:
        raise ValueError(
            "Can't merge %s and %s root tags" % (main_root, incoming_root))
    nsmap = incoming_root.nsmap # lxml returns us a copy, not an original
    nsmap.update(main_root.nsmap)
    new_root = etree.Element(
        main_root.tag,
        attrib=main_root.attrib,
        nsmap=nsmap)
    for ele in main_root.getchildren():
        new_root.append(ele)
    main_doc._setroot(new_root)
    return new_root

def inline_next_wsdl(loader : DocumentLoader, doc: etree._Document, url: str):
    """Import the the next WSDL include tag if its url hasn't been already
    TODO: Support importing other namespaces
    """
    root = doc.getroot()
    imports = (
        root.findall(f"{{{WSDL_NS}}}import") +
        root.findall(f"{{{WSDL_NS}}}include"))
    target_ns = root.attrib["targetNamespace"]
    for imp in imports:
        imp_url = None
        imp_ns = None

        if imp.tag == f"{{{WSDL_NS}}}include":
            if imp.attrib["namespace"] != target_ns:
                raise ValueError(
                    "Namespace mismatch: %s vs %s",
                    imp.attrib["namespace"], target_ns)
        imp_ns = imp.attrib["namespace"]
        try:
            imp_url = imp.attrib.get("location")
        except KeyError:
            LOGGER.exception("include tag without location in %s", url)
            raise

        imp_doc, imp_url_out = loader.load_xml(imp_url)
        if imp_doc is None:
            imp.getparent().replace(imp, etree.Comment(etree.tostring(imp)))
            return True, False
        references = get_references(imp_doc)
        fix_references(imp_doc, references, imp_url_out)

        imp_root = imp_doc.getroot()
        imp_doc_ns = imp_root.get("targetNamespace")
        if imp_doc_ns and imp_doc_ns != imp_ns:
            raise ValueError(
                "Asked to import %s:%s into %s:%s" % (
                    imp_url_out, imp_doc_ns, url, target_ns
                )
            )
        if imp_doc_ns != target_ns:
            ## TODO: assign targetNamespaces in first pass, then
            ## do a name fix after all recursion is over
            # imp_ele.attrib["targetNamespace"] = imp_doc_ns
            LOGGER.warning(
                "Skipping WSDL import %s:%s for %s:%s # TODO",
                imp_url_out, imp_doc_ns, url, target_ns)
            imp.addprevious(etree.Comment(
                "Not imported (%s != %s)", imp_doc_ns, target_ns))
            return True, False
        root = merge_root_nodes(doc, imp_doc)
        children = imp_root.getchildren() or []
        for imp_ele in reversed(children):
            imp.addnext(imp_ele)
        imp.addprevious(etree.Comment("Imported into same namespace:"))
        imp.getparent().replace(imp, etree.Comment(etree.tostring(imp)))
        return True, True
    return False, False

def inline_references(function, description, loader, doc, url):
    """Attempt to import all XSD files directly into the document

    Return value is a tuple - the first value is the number of XSD import
    tags modified, and the second is the number of unique files actually
    imported."""
    run = True
    modified, imported = 0, 0
    while run:
        run, new = function(loader, doc, url)
        if run:
            modified += 1
        if new:
            imported += 1
    LOGGER.info(
        "%s: imported %u %s files, modified %u tags",
        url, imported, description, modified)
    return modified, imported

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
        f".//{{{XSD_NS}}}schema")
    for schema in schemas:
        # Process the next import, removing the "schemaLocation" attribute.
        imports = schema.findall(f"{{{XSD_NS}}}import")
        for imp in imports:
            imp_url = imp.attrib.pop("schemaLocation", None)
            if imp_url is None:
                continue
            imp_doc, imp_url_out = loader.load_xml(imp_url)
            if imp_doc is None:
                return True, False
            references = get_references(imp_doc)
            fix_references(imp_doc, references, imp_url_out)
            tag_namespace = imp.attrib.get(
                "namespace",
                imp.getparent().attrib.get("targetNamespace"))
            imp_root = imp_doc.getroot()
            imp_namespace = imp_root.attrib.get("targetNamespace")
            schema.addprevious(imp_doc.getroot())
            return True, True

        # Process the next include, removing the include tag and replacing
        # it with the XSD content
        includes = schema.findall(f"{{{XSD_NS}}}include")
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
            fix_references(inc_doc, references, inc_url_out)
            children = inc_doc.getroot().getchildren() or []
            for element in reversed(children):
                inc.addnext(element)
            inc.getparent().replace(inc, etree.Comment(etree.tostring(inc)))
            return True, True
    return False, False

def inline_xsd_references(loader, doc, url):
    return inline_references(inline_next_xsd, "XSD", loader, doc, url)

def inline_wsdl_references(loader, doc, url):
    return inline_references(inline_next_wsdl, "WSDL", loader, doc, url)

def wsdl2dom(url: str, client_cert=None, keep_clark=False):
    "Convert the WSDL at `url` to a single DOM tree"
    session = Session(cert=client_cert)
    loader = ClarkDocumentLoader(session=session)
    doc, url_out = loader.load_xml(url)
    references = get_references(doc)
    fix_references(doc, references, url_out)
    inline_wsdl_references(loader, doc, url_out)
    inline_xsd_references(loader, doc, url_out)
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
        keep_clark=options.keep_clark)
    etree.indent(doc, space=' ', level=2)
    print(etree.tostring(doc.getroot()).decode())
    return(0)


if __name__ == "__main__":
    exit(main())