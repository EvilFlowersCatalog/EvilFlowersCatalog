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

from apps.core.modifiers import ModifierContext, InvalidPage


class PDFModifier:
    DEFAULT_CONTEXT: ModifierContext = {
        "generated_at": timezone.now().isoformat(),
        "instance": settings.INSTANCE_NAME,
    }

    def __init__(self, context: ModifierContext, pages: Optional[List]):
        self._context = self.DEFAULT_CONTEXT | context
        self._pages = pages or None

    def create_qr(self) -> bytes:
        qr = qrcode.QRCode(version=4, border=0, error_correction=qrcode.constants.ERROR_CORRECT_H)
        qr.add_data(json.dumps(self._context))
        qr = qr.make_image().get_image().convert("RGBA")
        qr.putalpha(150)

        stream = io.BytesIO()
        qr.save(stream, format="PNG")

        return stream.getvalue()

    def generate(self, file: File, page_num: Optional[int] = None) -> File:
        document = fitz.open(stream=file.read())

        document.set_metadata(
            document.metadata
            | {
                "author": self._context["authors"],
                "title": self._context["title"],
                "subject": f"{self._context['username']} ({self._context['user_id']})",
                "creator": f"EvilFlowers/{settings.INSTANCE_NAME}",
            }
        )

        if self._pages:
            document.select([i - 1 for i in self._pages])

        # Attempt to load the language-specific template, falling back to default if not found
        try:
            chosen_template = get_template(f"files/license_{self._context['language']}.html")
        except TemplateDoesNotExist:
            chosen_template = get_template("files/license.txt")

        # Render the chosen template with the provided context data
        document.insert_page(
            1,
            text=chosen_template.render(self._context),
            fontsize=11,
            width=595,
            height=842,
            fontname="Helvetica",  # default font
            fontfile=None,  # any font file name
            color=(0, 0, 0),
        )  # text color (RGB)

        # Add QR codes to rest of pages
        qr = self.create_qr()
        for index in range(2, len(document)):
            page = document[index]
            page.insert_image(
                fitz.Rect(10, page.mediabox.y1 - 50, 50, page.mediabox.y1 - 10),
                stream=io.BytesIO(qr),
            )

        if page_num:
            try:
                document.select([int(page_num) - 1])
            except ValueError:
                raise InvalidPage()

        return File(io.BytesIO(document.tobytes(garbage=3, deflate=True, deflate_images=True, linear=True)))
