from lxml import etree
import logging
import re

from wsdl2file.const import WSDL_NS, XSD_NS

LOGGER = logging.getLogger(__name__)

xmlschema_attribute_map = {
    f"{{{XSD_NS}}}attribute": ["type", "ref"],
    f"{{{XSD_NS}}}attributeGroup": ["ref"],
    f"{{{XSD_NS}}}element": [
        "type", "substitutionGroup", "ref",
        "{http://release.niem.gov/niem/appinfo/4.0/}appliesToTypes"],
    f"{{{XSD_NS}}}extension": ["base"],
    f"{{{XSD_NS}}}restriction": ["base"],
    f"{{{WSDL_NS}}}part": ["element"],
    f"{{{WSDL_NS}}}input": ["message"],
    f"{{{WSDL_NS}}}output": ["message"],
    f"{{{WSDL_NS}}}binding": ["type"],
}


def declark_tag(tag, attr_names):
    pmap = {v: k for k, v in tag.nsmap.items()}
    for attr_name in attr_names:
        if attr_name in tag.attrib:
            cids = tag.attrib[attr_name].split(' ')
            ids = []
            for cid in cids:
                match = re.match(r'^{(.+?)}(.+)$', cid)
                ns = match.group(1)
                attr = match.group(2)
                try:
                    prefix = pmap[ns]
                except KeyError as e:
                    LOGGER.error(
                        "Couldn't find prefix to declark: %s:%s",
                        etree.tostring(tag), attr_name)
                    raise
                if prefix == None:
                    ids.append(attr)
                else:
                    ids.append(f"{prefix}:{attr}")
            tag.attrib[attr_name] = ' '.join(ids)


def declark(ele, attribute_map=None):
    "Convert attributes from clark notation back to namespaced"
    attribute_map = attribute_map or xmlschema_attribute_map
    for tag_name, attr_names in attribute_map.items():
        if ele.tag == tag_name:
            declark_tag(ele, attr_names)
        for tag in ele.findall(f".//{tag_name}"):
            declark_tag(tag, attr_names)

def clark_tag(tag, attrs):
    "Convert each attribute listed in `attrs` to clark notation"
    for attr in attrs:
        if attr in tag.attrib:
            # spaces are not valid in identifier names, and some
            # attributes take advantage of this by allowing you to
            # supply a space-separated list of identifiers
            ids = tag.attrib[attr].split(' ')
            ido = []
            for idi in ids:
                if ':' in idi:
                    ns, val = idi.split(':', 2)
                    ido.append(f"{{{tag.nsmap[ns]}}}{val}")
                elif None in tag.nsmap:
                    ido.append(f"{{{tag.nsmap[None]}}}{idi}")
                else:
                    raise ValueError(
                        "Can't handle attribute %s on tag %s" % (
                        attr, etree.tostring(tag)))
            tag.attrib[attr]=" ".join(ido)

def clark(ele, attribute_map=None):
    "Convert the relevant attributes beneath `element` to Clark Notation"
    attribute_map = attribute_map or xmlschema_attribute_map
    for tag, attrs in attribute_map.items():
        if ele.tag == tag:
            clark_tag(ele, attrs)
        children = ele.findall(f".//{tag}")
        for child in children:
            clark_tag(child, attrs)
