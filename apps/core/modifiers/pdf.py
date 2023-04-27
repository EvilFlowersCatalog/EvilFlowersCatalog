import io
import json
from typing import Optional

import qrcode
from pypdf import PdfReader, PdfWriter, PageObject
from django.conf import settings
from django.core.files import File
from django.template.loader import render_to_string
from django.utils import timezone
import fpdf

from apps.core.modifiers import ModifierContext


class PDFModifier:
    DEFAULT_CONTEXT: ModifierContext = {
        'generated_at': timezone.now().isoformat(),
        'url': 'https://elvira.fiit.stuba.sk',
        'instance': settings.INSTANCE_NAME,
        'contact': settings.CONTACT_EMAIL
    }

    def __init__(self, context: ModifierContext):
        self._context = context

    def _intro_page(self) -> PageObject:
        pdf = fpdf.FPDF()
        pdf.add_page()

        pdf.write_html(
            render_to_string('files/intro.html', self.DEFAULT_CONTEXT | self._context),
        )

        result = PdfReader(io.BytesIO(pdf.output()))
        return result.pages[0]

    def create_qr(self) -> PageObject:
        pdf = fpdf.FPDF()
        qr = qrcode.QRCode(
            version=25,
            border=0,
            error_correction=qrcode.constants.ERROR_CORRECT_H
        )
        qr.add_data(json.dumps(self.DEFAULT_CONTEXT | self._context))
        qr = qr.make_image().get_image().convert('RGBA')
        qr.putalpha(150)

        pdf.add_page()
        pdf.image(qr, h=20, w=20, x=pdf.l_margin, y=pdf.eph)

        return PdfReader(io.BytesIO(pdf.output())).pages[0]

    def generate(self, file: File, page_num: Optional[int] = None) -> File:
        reader = PdfReader(file)
        writer = PdfWriter()

        # Add QR code to intro
        page = self.create_qr()
        page.merge_page(reader.pages[0])
        writer.add_page(page)

        # Add license page
        writer.add_page(self._intro_page())

        # Add QR codes to rest of pages
        for index, original_page in enumerate(reader.pages[1:]):
            page = self.create_qr()
            page.merge_page(original_page)
            writer.add_page(page)

        stream = io.BytesIO()
        if page_num:
            # FIXME: this requires serious optimisation
            single_page_writer = PdfWriter()
            single_page_writer.add_page(writer.pages[int(page_num) - 1])
            single_page_writer.write(stream)
        else:
            writer.write(stream)
        stream.seek(0)

        return File(stream)
