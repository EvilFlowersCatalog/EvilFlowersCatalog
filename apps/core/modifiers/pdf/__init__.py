import io
import json
from typing import Optional, List

import fitz
import qrcode
from django.conf import settings
from django.core.files import File
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.utils import timezone
import xml.etree.ElementTree as ET

from apps.core.modifiers import ModifierContext, InvalidPage
from apps.core.modifiers.pdf.annotations import shape_factory


class PDFModifier:
    # A4 in PostScript points (72 dpi) — geometry of the inserted license page.
    LICENSE_PAGE_WIDTH = 595
    LICENSE_PAGE_HEIGHT = 842

    # Save options tuned for the downstream consumers of the personalised copy:
    # the EvilFlowers viewer (pdf.js based, see elvira-portal `Viewer.tsx`) and
    # Readium readers. The viewer downloads the *entire* file into memory before
    # rendering, so producing the smallest well-formed file is the single biggest
    # UX lever we control here.
    #
    #   garbage=4        Full xref compaction and duplicate-object removal. Also
    #                    deduplicates the repeated QR watermark image.
    #   clean=True       Sanitise/rewrite content streams. Improves robustness in
    #                    pdf.js, which is stricter than Acrobat about malformed
    #                    streams produced by some source PDFs.
    #   deflate*         Compress streams, images and fonts.
    #   use_objstms=1    Pack indirect objects into object streams (PDF 1.5+),
    #                    typically 30–40% smaller than garbage collection alone.
    #                    MuPDF 1.29 (shipped with PyMuPDF 1.28) *removed*
    #                    linearisation ("fast web view"), so object streams are
    #                    the modern replacement for a web-optimised layout. Every
    #                    reader in this stack (pdf.js, Readium, Thorium) supports
    #                    them. `use_objstms` and `linear` are mutually exclusive.
    #   encryption=NONE  Strip any owner-password restrictions carried by the
    #                    source PDF so the viewer can render, search and (when the
    #                    catalog permits) print the licensed copy without hitting
    #                    permission errors. This is not the DRM boundary — Readium
    #                    LCP encryption is applied separately, to the raw
    #                    acquisition, by ContentEncryptionService.
    SAVE_OPTIONS = {
        "garbage": 4,
        "clean": True,
        "deflate": True,
        "deflate_images": True,
        "deflate_fonts": True,
        "use_objstms": 1,
        "encryption": fitz.PDF_ENCRYPT_NONE,
    }

    def __init__(self, context: ModifierContext, pages: Optional[List]):
        # Build the default context per-instance so ``generated_at`` reflects the
        # real generation time. It was previously a class attribute evaluated once
        # at import time, which froze every watermark/QR/license timestamp to the
        # worker process's boot time.
        default_context: ModifierContext = {
            "generated_at": timezone.now().isoformat(),
            "instance": settings.INSTANCE_NAME,
        }
        self._context = default_context | context
        self._pages = pages or None

    def create_qr(self) -> bytes:
        qr = qrcode.QRCode(version=4, border=0, error_correction=qrcode.constants.ERROR_CORRECT_H)
        qr.add_data(json.dumps(self._context))
        qr = qr.make_image().get_image().convert("RGBA")
        qr.putalpha(150)

        stream = io.BytesIO()
        qr.save(stream, format="PNG")

        return stream.getvalue()

    def generate(
        self, file: File, page_num: Optional[int] = None, annotation_map: dict[int, list[str]] = None
    ) -> File:
        document = fitz.open(stream=file.read())

        if not annotation_map:
            annotation_map = {}

        document.set_metadata(
            document.metadata
            | {
                "author": self._context.get("authors") or "",
                "title": self._context.get("title") or "",
                "subject": f"{self._context.get('username')} ({self._context.get('user_id')})",
                "creator": f"EvilFlowers/{settings.INSTANCE_NAME}",
                # Identify the producing pipeline; helps when debugging files in
                # the wild and shows sensible provenance in reader "Properties".
                "producer": f"PyMuPDF/{fitz.VersionBind} EvilFlowers/{settings.INSTANCE_NAME}",
            }
        )

        if self._pages:
            # PyMuPDF re-maps the outline (TOC) across select(), dropping entries
            # that point outside the retained range — no manual fix-up needed.
            document.select([i - 1 for i in self._pages])

        # Attempt to load the language-specific template, falling back to default if not found
        try:
            chosen_template = get_template(f"files/license_{self._context['language']}.html")
        except TemplateDoesNotExist:
            chosen_template = get_template("files/license.txt")

        # Render the chosen template with the provided context data. insert_page
        # automatically shifts existing outline entries to keep bookmarks pointing
        # at the correct pages after the license page is spliced in.
        document.insert_page(
            1,
            text=chosen_template.render(self._context),
            fontsize=11,
            width=self.LICENSE_PAGE_WIDTH,
            height=self.LICENSE_PAGE_HEIGHT,
            fontname="Helvetica",  # default font
            fontfile=None,  # any font file name
            color=(0, 0, 0),
        )  # text color (RGB)

        # If the source ships an outline, give the reader's contents panel a
        # navigable entry for the license page (1-based page 2). We only augment
        # an existing TOC to avoid manufacturing a one-item outline for PDFs that
        # never had one.
        toc = document.get_toc(simple=True)
        if toc:
            toc.append([1, "License / Access terms", 2])
            document.set_toc(toc)

        # Add QR codes to rest of pages. The watermark image is embedded once and
        # every subsequent page references the same xref, avoiding a fresh PNG
        # decode/embed per page on long documents.
        qr = self.create_qr()
        qr_xref = 0

        for index in range(2, len(document)):
            page = document[index]

            rect = fitz.Rect(10, page.mediabox.y1 - 50, 50, page.mediabox.y1 - 10)
            if qr_xref:
                page.insert_image(rect, xref=qr_xref)
            else:
                qr_xref = page.insert_image(rect, stream=io.BytesIO(qr))

            # annotation map is indexing pages from 1 and generated document is larger by license page
            page_annotations = annotation_map.get(index - 1, [])
            if page_annotations:
                context = page.new_shape()

                for page_annotation in page_annotations:
                    for element in ET.fromstring(page_annotation):
                        shape = shape_factory(element)
                        shape.draw(element, context)

                context.commit()

        if page_num:
            try:
                document.select([int(page_num) - 1])
            except ValueError:
                raise InvalidPage()

        return File(io.BytesIO(document.tobytes(**self.SAVE_OPTIONS)))
