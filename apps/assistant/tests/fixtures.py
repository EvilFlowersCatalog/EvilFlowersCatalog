"""A processed publication for the IP-030 tests: an active extraction run with sections, sentences, figures."""

from django.test import RequestFactory

from apps.core.models import Acquisition, AuthSource, Catalog, Entry, User
from apps.extraction.models import ExtractionRun, Paragraph, ParagraphLink, Region, Sentence

BBOX = {"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.3}

#: (page, section, text) in reading order.
FLUIDS = [
    (1, "1 Introduction", "Fluids flow. This book introduces the motion of fluids in pipes and channels."),
    (2, "1 Introduction", "A fluid is a substance that deforms continuously under shear stress, see Figure 1.1."),
    (3, "2 Bernoulli", "Bernoulli's principle relates pressure and speed. Pressure falls where the speed rises."),
    (4, "2 Bernoulli", "The Venturi tube in Obr. 2.1 shows the effect. Its throat is narrower than the inlet."),
    (5, "2 Bernoulli", "Section 1 defines a fluid; the Venturi effect follows from the conservation of energy."),
]


def user(name="reader"):
    source, _ = AuthSource.objects.get_or_create(name="database", driver=AuthSource.Driver.DATABASE)
    reader = User(username=name, name="Reader", surname="Tester", auth_source=source)
    reader.set_unusable_password()
    reader.save()
    return reader


def request_for(reader):
    request = RequestFactory().post("/assistant/v1/chats")
    request.user = reader
    return request


def publication(creator, catalog, title="Fluid Mechanics", paragraphs=FLUIDS, readium=False):
    """An entry with an active run: paragraphs (with sentences), a figure on page 4 and one on page 2."""
    entry = Entry.objects.create(creator=creator, catalog=catalog, title=title)
    if readium:
        entry.config = {"readium_enabled": True}
        entry.save()
    acquisition = Acquisition.objects.create(entry=entry, mime=Acquisition.AcquisitionMIME.PDF)
    run = ExtractionRun.objects.create(
        acquisition=acquisition,
        pipeline_version=1,
        status=ExtractionRun.Status.DONE,
        is_active=True,
        page_count=6,
        provenance={"paragraphs": {"embedding_model": "test@1"}},
    )
    rows = []
    for seq, (page, section, text) in enumerate(paragraphs):
        paragraph = Paragraph.objects.create(run=run, page=page, seq=seq, section=section, bbox=BBOX, text=text)
        start = 0
        for number, sentence in enumerate(part for part in text.split(". ") if part):
            end = min(len(text), text.find(sentence, start) + len(sentence) + 1)
            Sentence.objects.create(
                paragraph=paragraph,
                seq=number,
                start=start,
                end=end,
                page=page,
                quads=[{"x0": 0.1, "y0": 0.2 + number / 100, "x1": 0.9, "y1": 0.21 + number / 100}],
            )
            start = end + 1
        rows.append(paragraph)
    for before, after in zip(rows, rows[1:]):
        ParagraphLink.objects.create(source=before, target=after, kind=ParagraphLink.Kind.NEXT)
    venturi = Region.objects.create(
        run=run,
        kind=Region.Kind.FIGURE,
        page=4,
        seq=0,
        bbox=BBOX,
        image_path=f"extraction/{run.pk}/venturi.webp",
        # `caption` is what clients read: the printed caption when there is one (as ingestion sets it).
        caption="Obr. 2.1: Venturiho trubica a pokles tlaku v zúžení",
        printed_caption="Obr. 2.1: Venturiho trubica a pokles tlaku v zúžení",
    )
    shear = Region.objects.create(
        run=run,
        kind=Region.Kind.FIGURE,
        page=2,
        seq=0,
        bbox=BBOX,
        printed_caption="Figure 1.1: Shear on a fluid layer",
    )
    return entry, run, rows, venturi, shear


def catalog(creator, public=True, name="open"):
    return Catalog.objects.create(creator=creator, title=name.title(), url_name=name, is_public=public)
