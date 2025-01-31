import itertools
import operator
import re

import dataclasses
from dataclasses import dataclass, field
from typing import List, Tuple, Iterable, Dict

from robotoff import settings
from robotoff.products import ProductDataset
from robotoff.utils import get_logger
from robotoff.utils.es import generate_msearch_body

logger = get_logger(__name__)

SPLITTER_CHAR = {'(', ')', ',', ';', '[', ']', '-', '{', '}'}

# Food additives (EXXX) may be mistaken from one another, because of their edit distance proximity
BLACKLIST_RE = re.compile(r"(?:\d+(?:[,.]\d+)?\s*%)|(?:E ?\d{3,5}[a-z]*)|(?:[_•:0-9])")

OffsetType = Tuple[int, int]


class TokenLengthMismatchException(Exception):
    pass


@dataclass
class Ingredients:
    text: str
    normalized: str
    offsets: List[OffsetType] = field(default_factory=list)

    def iter_normalized_ingredients(self) -> Iterable[str]:
        for start, end in self.offsets:
            yield self.normalized[start:end]

    def get_ingredient(self, index) -> str:
        start, end = self.offsets[index]
        return self.text[start:end]

    def get_normalized_ingredient(self, index) -> str:
        start, end = self.offsets[index]
        return self.normalized[start:end]

    def ingredient_count(self) -> int:
        return len(self.offsets)


@dataclass
class TermCorrection:
    original: str
    correction: str
    start_offset: int
    end_offset: int


@dataclass
class Correction:
    term_corrections: List[TermCorrection]
    score: int


def normalize_ingredients(ingredient_text: str):
    normalized = ingredient_text

    while True:
        try:
            match = next(BLACKLIST_RE.finditer(normalized))
        except StopIteration:
            break

        if match:
            start = match.start()
            end = match.end()
            normalized = normalized[:start] + ' ' * (end - start) + normalized[end:]
        else:
            break

    return normalized


def process_ingredients(ingredient_text: str) -> Ingredients:
    offsets = []
    chars = []

    normalized = normalize_ingredients(ingredient_text)
    start_idx = 0

    for idx, char in enumerate(normalized):
        if char in SPLITTER_CHAR:
            offsets.append((start_idx, idx))
            start_idx = idx + 1
            chars.append(' ')
        else:
            chars.append(char)

    if start_idx != len(normalized):
        offsets.append((start_idx, len(normalized)))

    normalized = ''.join(chars)
    return Ingredients(ingredient_text, normalized, offsets)


def generate_corrections(client, ingredients_text: str, **kwargs) -> List[Correction]:
    corrections = []
    ingredients: Ingredients = process_ingredients(ingredients_text)
    normalized_ingredients: Iterable[str] = ingredients.iter_normalized_ingredients()

    for idx, suggestions in enumerate(_suggest_batch(client, normalized_ingredients, **kwargs)):
        offsets = ingredients.offsets[idx]
        normalized_ingredient = ingredients.get_normalized_ingredient(idx)
        options = suggestions['options']

        if not options:
            continue

        option = options[0]
        original_tokens = analyze(client, normalized_ingredient)
        suggestion_tokens = analyze(client, option['text'])
        try:
            term_corrections = format_corrections(original_tokens,
                                                  suggestion_tokens,
                                                  offsets[0])
            corrections.append(Correction(term_corrections, option['score']))
        except TokenLengthMismatchException:
            logger.warning("The original text and the suggestions must have the same number "
                           "of tokens: {} / {}".format(original_tokens, suggestion_tokens))
            continue

    return corrections


def generate_corrected_text(corrections: List[TermCorrection], text: str):
    sorted_corrections = sorted(corrections,
                                key=operator.attrgetter('start_offset'))
    corrected_fragments = []

    last_correction = None
    for correction in sorted_corrections:
        if last_correction is None:
            corrected_fragments.append(text[:correction.start_offset])
        else:
            corrected_fragments.append(
                text[last_correction.end_offset:correction.start_offset])

        corrected_fragments.append(correction.correction)
        last_correction = correction

    if last_correction is not None:
        corrected_fragments.append(text[last_correction.end_offset:])

    return ''.join(corrected_fragments)


def format_corrections(original_tokens: List[Dict],
                       suggestion_tokens: List[Dict],
                       offset: int = 0):
    corrections = []

    if len(original_tokens) != len(suggestion_tokens):
        raise TokenLengthMismatchException()

    for original_token, suggestion_token in zip(original_tokens, suggestion_tokens):
        original_token_str = original_token['token']
        suggestion_token_str = suggestion_token['token']

        if original_token_str.lower() != suggestion_token_str:
            if original_token_str.isupper():
                token_str = suggestion_token_str.upper()
            elif original_token_str.istitle():
                token_str = suggestion_token_str.capitalize()
            else:
                token_str = suggestion_token_str

            token_start = original_token['start_offset']
            token_end = original_token['end_offset']
            corrections.append(TermCorrection(original=original_token_str,
                                              correction=token_str,
                                              start_offset=offset + token_start,
                                              end_offset=offset + token_end))

    return corrections


def _suggest(client, text):
    suggester_name = "autocorrect"
    body = generate_suggest_query(text, name=suggester_name)
    response = client.search(index='product',
                             doc_type='document',
                             body=body,
                             _source=False)
    return response['suggest'][suggester_name]


def analyze(client, ingredient_text: str):
    r = client.indices.analyze(index=settings.ELASTICSEARCH_PRODUCT_INDEX,
                               body={
                                   'tokenizer': "standard",
                                   'text': ingredient_text
                               })
    return r['tokens']


def _suggest_batch(client, texts: Iterable[str], **kwargs) -> List[Dict]:
    suggester_name = "autocorrect"
    queries = (generate_suggest_query(text, name=suggester_name, **kwargs)
               for text in texts)
    body = generate_msearch_body(settings.ELASTICSEARCH_PRODUCT_INDEX, queries)
    response = client.msearch(body=body,
                              doc_type=settings.ELASTICSEARCH_TYPE)

    suggestions = []

    for r in response['responses']:
        if r['status'] != 200:
            root_cause = response['error']['root_cause'][0]
            error_type = root_cause['type']
            error_reason = root_cause['reason']
            print("Elasticsearch error: {} [{}]"
                  "".format(error_reason, error_type))
            continue

        suggestions.append(r['suggest'][suggester_name][0])

    return suggestions


def generate_suggest_query(text,
                           confidence=1,
                           size=1,
                           min_word_length=4,
                           suggest_mode="popular",
                           name="autocorrect"):
    return {
        "suggest": {
            "text": text,
            name: {
                "phrase": {
                    "confidence": confidence,
                    "field": "ingredients_text_fr.trigram",
                    "size": size,
                    "gram_size": 3,
                    "direct_generator": [
                        {
                            "field": "ingredients_text_fr.trigram",
                            "suggest_mode": suggest_mode,
                            "min_word_length": min_word_length
                        }
                    ],
                    "smoothing": {
                        "laplace": {
                            "alpha": 0.5
                        }
                    }
                }
            }
        }
    }


def generate_insights(client, confidence=1):
    dataset = ProductDataset(settings.JSONL_DATASET_PATH)

    product_iter = (dataset.stream()
                    .filter_by_country_tag('en:france')
                    .filter_nonempty_text_field('ingredients_text_fr')
                    .iter())

    for product in product_iter:
        text = product['ingredients_text_fr']
        corrections = generate_corrections(client,
                                           text,
                                           confidence=confidence)

        if not corrections:
            continue

        term_corrections = list(itertools.chain
                                .from_iterable((c.term_corrections
                                                for c in corrections)))

        yield {
            'corrections': [dataclasses.asdict(c) for c in term_corrections],
            'text': text,
            'corrected': generate_corrected_text(term_corrections, text),
            'barcode': product['code'],
        }
