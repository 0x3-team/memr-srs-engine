"""ADR-03 card renders backed by the pinned Anki template and scheduler APIs.

Type-answer filtering follows anki 26.9.3's aqt/reviewer.py (Ankitects and
contributors, AGPL-3.0-or-later). Desktop hooks and pycmd handlers belong to
the browser runtime; comparison and cloze extraction remain in Anki's core.
"""
from dataclasses import dataclass
from html import escape, unescape
from html.entities import html5
import re
from urllib.parse import quote
from .provenance_links import first_source, parse_source_uri

# Import collection first: Anki's cards/hooks modules otherwise form a cycle.
from anki.collection import Collection
from anki.cards import Card
from anki.sound import SoundOrVideoTag, TTSTag
from anki.template import TemplateRenderContext

TYPE_PATTERN = re.compile(r"\[\[type:(.+?)\]\]")


@dataclass
class _TypedField:
    field: str
    correct: str | None
    font: str = ""
    size: int = 20
    combining: bool = True
    cloze: bool = False

    def metadata(self):
        if not self.correct:
            return None
        return {"field": self.field, "correct": self.correct,
                "font": self.font, "size": self.size}


def _typed_field(col, card, question):
    match = TYPE_PATTERN.search(question)
    if not match:
        return None
    field = match.group(1)
    cloze = field.startswith("cloze:")
    if cloze:
        field = field.split(":")[1]
    combining = not field.startswith("nc:")
    if not combining:
        field = field.split(":")[1]
    result = _TypedField(field, None, combining=combining, cloze=cloze)
    for definition in card.note_type()["flds"]:
        if definition["name"] == field:
            correct = card.note()[field]
            if cloze:
                correct = col.extract_cloze_for_typing(correct, card.ord + 1) or None
            result.correct = correct
            result.font = definition["font"]
            result.size = definition["size"]
            break
    return result


def _font_style(typed):
    # Escape the CSS string before escaping the enclosing HTML attribute.
    font = re.sub(r"['\\\r\n\f]", lambda m: f"\\{ord(m.group()):x} ", typed.font)
    return escape(f"font-family: '{font}'; font-size: {typed.size}px", quote=True)


def _question_html(col, question, typed):
    if typed is None:
        return question
    if not typed.correct:
        replacement = ""
        if typed.correct is None:
            warning = (col.tr.studying_please_run_toolsempty_cards() if typed.cloze
                       else col.tr.studying_type_answer_unknown_field(val=typed.field))
            replacement = escape(warning)
    else:
        replacement = ("\n<center>\n<input type=text id=typeans "
                       f'onkeypress="_typeAnsPress();" style="{_font_style(typed)};">'
                       "\n</center>\n")
    # A callable preserves backslashes in fonts and warning field names.
    return TYPE_PATTERN.sub(lambda _match: replacement, question)


def _answer_html(col, answer, typed, provided):
    if typed is None or not typed.correct:
        return TYPE_PATTERN.sub("", answer), ""
    comparison = col.compare_answer(typed.correct, provided, typed.combining)
    without_separator = answer.replace("<hr id=answer>", "")
    had_separator = without_separator != answer
    # Preserve the separator if the answer template has no type placeholder.
    if had_separator and not TYPE_PATTERN.search(without_separator):
        return answer, comparison
    replacement = f'\n<div style="{_font_style(typed)}">{comparison}</div>'
    if had_separator:
        replacement = "<hr id=answer>" + replacement
    return TYPE_PATTERN.sub(lambda _match: replacement, without_separator), comparison


def _av_tag(tag):
    if isinstance(tag, SoundOrVideoTag):
        return {"kind": "sound", "filename": tag.filename, "text": None,
                "language": None, "voices": [], "speed": 1.0}
    if isinstance(tag, TTSTag):
        return {"kind": "tts", "filename": None, "text": tag.field_text,
                "language": tag.lang, "voices": list(tag.voices), "speed": tag.speed}
    raise TypeError("Unsupported Anki AV tag")


def _source(note):
    return first_source(note)


def _source_uri(uri):
    return parse_source_uri(uri)


def _owned_body_class(col, card):
    # Names and copied metadata do not transfer the canonical type identity.
    model = card.note_type()
    marker = model.get("canonnotesProvenance")
    index = col.get_config("canonnotes.provenanceNotetypes")
    kinds = ("basic", "basicReversed", "cloze", "typeIn")
    if (type(index) is not dict or set(index) != {"version", "ids"}
            or type(index["version"]) is not int or index["version"] != 1
            or type(index["ids"]) is not dict or set(index["ids"]) - set(kinds)
            or any(type(value) is not int or not 0 < value <= 9007199254740991
                   for value in index["ids"].values())):
        return ""
    if (type(marker) is dict and marker.get("owner") == "canonnotes"
            and marker.get("kind") in kinds
            and type(marker.get("schemaVersion")) is int and marker["schemaVersion"] == 1
            and index["ids"].get(marker.get("kind")) == model["id"]):
        return " memr-owned"
    return ""


_OCCLUSION_IMAGE = re.compile(r'<img\s+[^>]*src\s*=\s*"([^"]+)"')
_CHARACTER_REFERENCE = re.compile(r"&(?:#[0-9]+;?|#[xX][0-9A-Fa-f]+;?|[A-Za-z0-9]+;?)")


def _attribute_text(raw: str) -> str:
    """Decode character references in a double-quoted attribute value as an HTML parser does.

    The legacy names that exist without a semicolon ("&amp", "&not") are decoded only when no
    letter, digit or "=" follows, so "q&notes.png" stays literal while "a&amp;b.png" becomes
    "a&b.png". Python's html.unescape lacks that attribute rule and would turn the first into
    "q\u00acs.png".
    """
    def decode(found):
        reference = found[0]
        if reference[1] == "#":
            return unescape(reference)
        token = reference[1:]
        for end in range(len(token), 0, -1):
            candidate = token[:end]
            if candidate in html5:
                follower = raw[found.start() + 1 + end:found.start() + 2 + end]
                literal = not candidate.endswith(";") and (follower == "=" or (follower.isascii() and follower.isalnum()))
                return reference if literal else html5[candidate] + token[end:]
        return reference

    return _CHARACTER_REFERENCE.sub(decode, raw)


def _occlusion_image_urls(col: Collection, card: Card, *rendered: str):
    """Encode an image-occlusion image's name once, where the browser reads it as a URL.

    Anki stores the media name literally in the Image field (and the note editor, getNote and
    updateNote treat it that way), but the card frame resolves the rendered src against the media
    prefix and the gateway decodes that path exactly once. A literal "50%25.png" would therefore
    load "50%.png" and a literal "50%.png" would be a malformed escape. An imported field is HTML
    text, so src="a&amp;b.png" names "a&b.png": the browser decodes the entity first, so the engine
    decodes it once here and only then percent-encodes the name. Only the browser-facing HTML
    changes; the stored field and the exported note keep what they hold.
    """
    model = card.note_type()
    if model.get("originalStockKind") != 6:
        return rendered
    field = card.note().fields[col._backend.get_image_occlusion_fields(model["id"]).image]
    found = _OCCLUSION_IMAGE.search(field)
    if not found:
        return rendered
    raw = found[1]
    name = _attribute_text(raw)
    if re.search(r"[/\\:?]", name):
        return rendered
    encoded = quote(name, safe="")
    if encoded == raw:
        return rendered
    return tuple(html.replace(f'src="{raw}"', f'src="{encoded}"') for html in rendered)


def render_card(col: Collection, card: Card, states=None, counts=None, fill_empty=False):
    """Return exactly cardRender's keys; queued callers pass their snapshot.

    Persisted cards default to native scheduling states and selected-deck
    counts. Ephemeral template previews have no scheduler state and zero
    counts. fill_empty uses Anki's layout renderer without editing the note.
    Auto advance starts disabled, matching the desktop's session toggle;
    timings/actions are supplied for the UI to use when the user enables it.
    """
    if fill_empty:
        # As in Note.ephemeral_card(), cloze layouts reuse template zero but
        # must render the card's actual cloze ordinal.
        template = dict(card.template())
        template["ord"] = card.ord
        output = TemplateRenderContext.from_card_layout(
            card.note(), card, card.note_type(), template, fill_empty=True,
        ).render()
    else:
        output = card.render_output()
    typed = _typed_field(col, card, output.question_text)
    question = _question_html(col, output.question_text, typed)
    answer, _comparison = _answer_html(col, output.answer_text, typed, "")
    question, answer = _occlusion_image_urls(col, card, question, answer)
    if states is None and card.id:
        states = col._backend.get_scheduling_states(card.id)
    next_states = []
    if states is not None:
        labels = col.sched.describe_next_states(states)
        for rating, name in enumerate(("again", "hard", "good", "easy"), 1):
            next_states.append({"rating": rating, "label": labels[rating - 1],
                                "secondsUntilDue": col.sched._interval_for_state(getattr(states, name))})
    if counts is None:
        counts = (dict(zip(("new", "learning", "review"), col.sched.counts())) if card.id
                  else {"new": 0, "learning": 0, "review": 0})
    config = col.decks.config_dict_for_deck_id(card.current_deck_id())
    # These ordinals are aqt.reviewer's enums, not the rating enum.
    question_action = {0: "showAnswer", 1: "none"}.get(config["questionAction"], "showAnswer")
    answer_action = {0: "bury", 1: "again", 2: "good", 3: "hard", 4: "none"}.get(config["answerAction"], "bury")
    return {
        "cardId": int(card.id), "noteId": int(card.note().id), "ord": card.ord,
        "deckId": int(card.did), "queue": card.queue,
        "questionHtml": question, "answerHtml": answer, "css": output.css,
        # Anki's invariant classes activate the template's .card CSS. The
        # browser runtime supplies its own platform and theme classes.
        "bodyClass": f"card card{card.ord + 1}" + _owned_body_class(col, card),
        "av": {"question": [_av_tag(tag) for tag in output.question_av_tags],
               "answer": [_av_tag(tag) for tag in output.answer_av_tags]},
        "typeAnswer": typed.metadata() if typed else None,
        "autoplay": bool(config["autoplay"]), "replayQuestionOnAnswer": bool(config.get("replayq", True)),
        "timer": {"show": bool(config["timer"]), "limitSeconds": config["maxTaken"]},
        "autoAdvance": {"enabled": False,
                        "questionSeconds": config["secondsToShowQuestion"],
                        "answerSeconds": config["secondsToShowAnswer"],
                        "questionAction": question_action, "answerAction": answer_action,
                        "waitForAudio": bool(config["waitForAudio"])},
        "nextStates": next_states, "counts": dict(counts), "flags": card.user_flag(),
        "marked": card.note().has_tag("marked"), "source": _source(card.note()),
    }


def compare_typed_answer(col: Collection, card: Card, typed_answer: str):
    """Render the answer with Anki's comparison, using the question's field.

    comparisonHtml is the unwrapped col.compare_answer result; answerHtml
    contains the font wrapper and the upstream FrontSide separator handling.
    """
    output = card.render_output()
    typed = _typed_field(col, card, output.question_text)
    answer, comparison = _answer_html(col, output.answer_text, typed, typed_answer)
    return {"answerHtml": answer, "comparisonHtml": comparison}
