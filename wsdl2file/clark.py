from lxml import etree
import re

xmlschema_attribute_map = {
    "{http://www.w3.org/2001/XMLSchema}attribute": ["type", "ref"],
    "{http://www.w3.org/2001/XMLSchema}attributeGroup": ["ref"],
    "{http://www.w3.org/2001/XMLSchema}element": [
        "type", "substitutionGroup", "ref",
        "{http://release.niem.gov/niem/appinfo/4.0/}appliesToTypes"],
    "{http://www.w3.org/2001/XMLSchema}extension": ["base"],
    "{http://www.w3.org/2001/XMLSchema}restriction": ["base"],
    "{http://schemas.xmlsoap.org/wsdl/}part": ["element"],
    "{http://schemas.xmlsoap.org/wsdl/}input": ["message"],
    "{http://schemas.xmlsoap.org/wsdl/}output": ["message"],
    "{http://schemas.xmlsoap.org/wsdl/}binding": ["type"],
}

def declark_tag(tag, attr_names):
    for attr_name in attr_names:
        if attr_name in tag.attrib:
            pmap = {v: k for k, v in tag.nsmap.items()}
            cids = tag.attrib[attr_name].split(' ')
            ids = []
            for cid in cids:
                match = re.match(r'^{(.+?)}(.+)$', cid)
                ns = match.group(1)
                attr = match.group(2)
                prefix = pmap[ns]
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
