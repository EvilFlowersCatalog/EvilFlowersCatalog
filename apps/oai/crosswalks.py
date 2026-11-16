"""IP-033 crosswalks: a stored projection rendered as `oai_dc` or `marc21` (MARCXML).

They read only the projection and the record's datestamp, so what is served is exactly what was fingerprinted.
A change to what they emit must bump `apps.oai.projection.CROSSWALK_VERSION`.
"""

from datetime import datetime, timezone
from typing import Optional

from lxml import etree

from apps.oai.projection import text

XSI = "http://www.w3.org/2001/XMLSchema-instance"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

#: MARC 856 $y: the link text a catalogue shows.
PORTAL_LINK_TEXT = "Požičať v Elvíre"


class Crosswalk:
    prefix: str
    namespace: str
    schema: str

    def render(self, projection: dict, datestamp: datetime) -> etree._Element:
        raise NotImplementedError


def _year(published_at: Optional[str]) -> Optional[str]:
    year = (published_at or "")[:4]
    return year if len(year) == 4 and year.isdigit() else None


class OaiDc(Crosswalk):
    prefix = "oai_dc"
    namespace = "http://www.openarchives.org/OAI/2.0/oai_dc/"
    schema = "http://www.openarchives.org/OAI/2.0/oai_dc.xsd"
    DC = "http://purl.org/dc/elements/1.1/"

    def render(self, projection: dict, datestamp: datetime) -> etree._Element:
        root = etree.Element(f"{{{self.namespace}}}dc", nsmap={"oai_dc": self.namespace, "dc": self.DC, "xsi": XSI})
        root.set(f"{{{XSI}}}schemaLocation", f"{self.namespace} {self.schema}")

        def add(name: str, value, lang: Optional[str] = None):
            value = text(value)
            if value:
                element = etree.SubElement(root, f"{{{self.DC}}}{name}")
                element.text = value
                if lang:
                    element.set(XML_LANG, lang)

        add("title", projection.get("title"))
        for author in projection.get("authors") or []:
            add("creator", ", ".join(part for part in (author.get("surname"), author.get("name")) if part))
        for subject in projection.get("subjects") or []:
            code = subject.get("term")
            if subject.get("scheme_name") and code:
                code = f"{subject['scheme_name']} {code}"
            labels = subject.get("labels") or {}
            if code and code not in labels.values():
                add("subject", code)
            for lang, label in labels.items():
                add("subject", label, lang)
        add("description", projection.get("summary"))
        add("publisher", projection.get("publisher"))
        add("date", projection.get("published_at"))
        add("type", "Text")
        for mime in projection.get("formats") or []:
            add("format", mime)
        identifiers = projection.get("identifiers") or {}
        add("identifier", projection.get("url"))
        if identifiers.get("isbn"):
            add("identifier", f"urn:isbn:{identifiers['isbn']}")
        if identifiers.get("issn"):
            add("identifier", f"urn:issn:{identifiers['issn']}")
        if identifiers.get("doi"):
            add("identifier", f"https://doi.org/{identifiers['doi']}")
        language = projection.get("language") or {}
        add("language", language.get("alpha2"))
        for external in projection.get("external_ids") or []:
            add("relation", external.get("url"))
        rights = projection.get("rights") or {}
        add("rights", rights.get("license_url"))
        if rights.get("license_name") and rights.get("license_name") != rights.get("license_url"):
            add("rights", rights.get("license_name"))
        add("rights", (projection.get("access") or {}).get("statement"))
        return root


class Marc21(Crosswalk):
    """MARCXML per guidelines-marcxml; mapping in the IP-033 table, reviewed with the STU library later (Q4)."""

    prefix = "marc21"
    namespace = "http://www.loc.gov/MARC21/slim"
    schema = "http://www.loc.gov/standards/marcxml/schema/MARC21slim.xsd"
    UDC_SCHEME = "http://udcdata.info/"

    #: New, language material, monograph, Unicode; encoding level 3 (abbreviated: generated, not catalogued
    #: here), ISBD punctuation omitted (c). Length and base address are left zero, as MARCXML allows.
    LEADER = "00000nam a22000003c 4500"
    #: Electronic resource: online (006/00 m, /06 o, /09 d document) and a remote computer file (007).
    FIELD_006 = "m     o  d        "
    FIELD_007 = "cr |||||||||||"

    def render(self, projection: dict, datestamp: datetime) -> etree._Element:
        root = etree.Element(f"{{{self.namespace}}}record", nsmap={"marc": self.namespace, "xsi": XSI})
        root.set(f"{{{XSI}}}schemaLocation", f"{self.namespace} {self.schema}")
        root.set("type", "Bibliographic")
        etree.SubElement(root, f"{{{self.namespace}}}leader").text = self.LEADER

        def control(tag: str, value: str):
            element = etree.SubElement(root, f"{{{self.namespace}}}controlfield", tag=tag)
            element.text = value

        def data(tag: str, ind1: str, ind2: str, *subfields):
            subfields = [(code, text(value)) for code, value in subfields if text(value)]
            if not subfields:
                return
            element = etree.SubElement(root, f"{{{self.namespace}}}datafield", tag=tag, ind1=ind1, ind2=ind2)
            for code, value in subfields:
                etree.SubElement(element, f"{{{self.namespace}}}subfield", code=code).text = value

        org = projection.get("marc_org_code") or "ELVIRA"
        identifiers = projection.get("identifiers") or {}
        language = (projection.get("language") or {}).get("alpha3")
        authors = projection.get("authors") or []
        year = _year(projection.get("published_at"))

        control("001", projection["entry_id"])
        control("003", org)
        control("005", datestamp.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S.0"))
        control("006", self.FIELD_006)
        control("007", self.FIELD_007)
        control("008", self._field_008(projection.get("created"), year, language))

        data("015", " ", " ", ("a", identifiers.get("ccnb")), ("2", "cnb" if identifiers.get("ccnb") else None))
        data("020", " ", " ", ("a", identifiers.get("isbn")))
        data("022", " ", " ", ("a", identifiers.get("issn")))
        data("024", "7", " ", ("a", identifiers.get("doi")), ("2", "doi" if identifiers.get("doi") else None))
        data("035", " ", " ", ("a", f"({org}){projection['entry_id']}"))
        for external in projection.get("external_ids") or []:
            data("035", " ", " ", ("a", f"({external.get('marc_org_code')}){external.get('record_id')}"))
        data("041", " ", " ", ("a", language))
        for subject in projection.get("subjects") or []:
            if subject.get("scheme") == self.UDC_SCHEME:
                data("080", " ", " ", ("a", subject.get("term")))

        names = [", ".join(part for part in (a.get("surname"), a.get("name")) if part) for a in authors]
        names = [name for name in names if name]
        if names:
            data("100", "1", " ", ("a", names[0]))
        # 245 ind1: 1 when a 1XX main entry exists, 0 when the title is the main entry.
        data("245", "1" if names else "0", "0", ("a", projection.get("title")))
        data("250", " ", " ", ("a", projection.get("edition")))
        data("264", " ", "1", ("b", projection.get("publisher")), ("c", year))
        if projection.get("page_count"):
            data("300", " ", " ", ("a", f"1 online resource ({projection['page_count']} pages)"))
        access = projection.get("access") or {}
        data("506", "1", " ", ("a", access.get("statement")))
        data("520", " ", " ", ("a", projection.get("summary")))
        rights = projection.get("rights") or {}
        data("540", " ", " ", ("a", rights.get("license_name")), ("u", rights.get("license_url")))
        for subject in projection.get("subjects") or []:
            data("653", " ", " ", ("a", subject.get("label") or subject.get("term")))
        for name in names[1:]:
            data("700", "1", " ", ("a", name))
        if projection.get("url"):
            data("856", "4", "0", ("u", projection["url"]), ("y", PORTAL_LINK_TEXT))
        return root

    @staticmethod
    def _field_008(created: Optional[str], year: Optional[str], language: Optional[str]) -> str:
        entered = created[2:4] + created[5:7] + created[8:10] if created and len(created) >= 10 else "||||||"
        field = (
            entered  # 00-05 date entered on file
            + ("s" + year if year else "nuuuu")  # 06 date type, 07-10 date 1
            + "    "  # 11-14 date 2
            + "xx "  # 15-17 place of publication: unknown
            + "    "  # 18-21 illustrations
            + " "  # 22 target audience
            + "o"  # 23 form of item: online
            + "    "  # 24-27 nature of contents
            + " "  # 28 government publication
            + "0"  # 29 conference publication
            + "0"  # 30 festschrift
            + "0"  # 31 index
            + " "  # 32 undefined
            + "0"  # 33 literary form: not fiction
            + " "  # 34 biography
            + ((language or "und")[:3].ljust(3))  # 35-37 language
            + " "  # 38 modified record
            + "d"  # 39 cataloging source: other
        )
        return field


CROSSWALKS: dict[str, Crosswalk] = {crosswalk.prefix: crosswalk for crosswalk in (OaiDc(), Marc21())}


__all__ = ["CROSSWALKS", "Crosswalk", "Marc21", "OaiDc", "PORTAL_LINK_TEXT"]
