import abc
import pathlib
from typing import Dict, List, Optional

from robotoff import settings
from robotoff.insights._enum import InsightType
from robotoff.models import ProductInsight
from robotoff.off import get_product
from robotoff.taxonomy import TaxonomyType, Taxonomy, get_taxonomy
from robotoff.utils import get_logger
from robotoff.utils.i18n import TranslationStore
from robotoff.utils.types import JSONType

logger = get_logger(__name__)


LABEL_IMG_BASE_URL = "https://static.openfoodfacts.org/images/lang"

LABEL_IMAGES = {
    "en:eu-organic": LABEL_IMG_BASE_URL + "en/labels/eu-organic.135x90.svg",
    "fr:ab-agriculture-biologique": LABEL_IMG_BASE_URL + "/fr/labels/ab-agriculture-biologique.74x90.svg",
    "en:european-vegetarian-union": LABEL_IMG_BASE_URL + "/en/labels/european-vegetarian-union.90x90.svg",
    "en:pgi": LABEL_IMG_BASE_URL + "/en/labels/pgi.90x90.png",
}


class Question(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def serialize(self) -> JSONType:
        pass

    @abc.abstractmethod
    def get_type(self):
        pass


class AddBinaryQuestion(Question):
    def __init__(self, question: str,
                 value: str,
                 insight: ProductInsight,
                 image_url: Optional[str] = None,
                 source_image_url: Optional[str] = None):
        self.question: str = question
        self.value: str = value
        self.insight_id: str = str(insight.id)
        self.insight_type: str = str(insight.type)
        self.barcode: str = insight.barcode
        self.image_url: Optional[str] = image_url
        self.source_image_url: Optional[str] = source_image_url

    def get_type(self):
        return 'add-binary'

    def serialize(self) -> JSONType:
        serial = {
            'barcode': self.barcode,
            'type': self.get_type(),
            'value': self.value,
            'question': self.question,
            'insight_id': self.insight_id,
            'insight_type': self.insight_type,
        }

        if self.image_url:
            serial['image_url'] = self.image_url

        if self.source_image_url:
            serial['source_image_url'] = self.source_image_url

        return serial


class QuestionFormatter(metaclass=abc.ABCMeta):
    def __init__(self, translation_store: TranslationStore):
        self.translation_store: TranslationStore = translation_store

    @abc.abstractmethod
    def format_question(self, insight: ProductInsight, lang: str) -> Question:
        pass


class CategoryQuestionFormatter(QuestionFormatter):
    question = "Does the product belong to this category?"

    def format_question(self, insight: ProductInsight, lang: str) -> Question:
        value: str = insight.value_tag
        taxonomy: Taxonomy = get_taxonomy(TaxonomyType.category.name)
        localized_value: str = taxonomy.get_localized_name(value, lang)
        localized_question = self.translation_store.gettext(lang, self.question)
        source_image_url = self.get_source_image_url(insight.barcode)
        return AddBinaryQuestion(question=localized_question,
                                 value=localized_value,
                                 insight=insight,
                                 source_image_url=source_image_url)

    @staticmethod
    def get_source_image_url(barcode: str) -> Optional[str]:
        product: Optional[JSONType] = get_product(barcode,
                                                  fields=['selected_images'])

        if product is None:
            return None

        if 'selected_images' not in product:
            return None

        selected_images = product['selected_images']

        if 'front' not in selected_images:
            return None

        front_images = selected_images['front']

        if 'display' in front_images:
            display_images = list(front_images['display'].values())

            if display_images:
                return display_images[0]

        return None


class ProductWeightQuestionFormatter(QuestionFormatter):
    question = "Does this weight match the weight displayed on the product?"

    def format_question(self, insight: ProductInsight, lang: str) -> Question:
        value: str = insight.data['text']
        localized_question = self.translation_store.gettext(lang, self.question)
        source_image_url = (settings.OFF_IMAGE_BASE_URL +
                            get_display_image(insight.source_image))

        return AddBinaryQuestion(question=localized_question,
                                 value=value,
                                 insight=insight,
                                 source_image_url=source_image_url)


class LabelQuestionFormatter(QuestionFormatter):
    question = "Does the product have this label?"

    def format_question(self, insight: ProductInsight, lang: str) -> Question:
        value: str = insight.data['label_tag']

        image_url = None

        if value in LABEL_IMAGES:
            image_url = LABEL_IMAGES[value]

        taxonomy: Taxonomy = get_taxonomy(TaxonomyType.label.name)
        localized_value: str = taxonomy.get_localized_name(value, lang)
        localized_question = self.translation_store.gettext(lang, self.question)
        source_image_url = (settings.OFF_IMAGE_BASE_URL +
                            get_display_image(insight.source_image))

        return AddBinaryQuestion(question=localized_question,
                                 value=localized_value,
                                 insight=insight,
                                 image_url=image_url,
                                 source_image_url=source_image_url)


class BrandQuestionFormatter(QuestionFormatter):
    question = "Does the product belong to this brand?"

    def format_question(self, insight: ProductInsight, lang: str) -> Question:
        value: str = insight.data['brand']
        localized_question = self.translation_store.gettext(lang, self.question)
        source_image_url = (settings.OFF_IMAGE_BASE_URL +
                            get_display_image(insight.source_image))

        return AddBinaryQuestion(question=localized_question,
                                 value=value,
                                 insight=insight,
                                 source_image_url=source_image_url)


def get_display_image(source_image: str) -> str:
    image_path = pathlib.Path(source_image)

    if not image_path.stem.isdigit():
        return source_image

    display_name = "{}.400.jpg".format(image_path.name.split('.')[0])
    return str(image_path.parent / display_name)


class QuestionFormatterFactory:
    formatters: Dict[str, type] = {
        InsightType.category.name: CategoryQuestionFormatter,
        InsightType.label.name: LabelQuestionFormatter,
        InsightType.product_weight.name: ProductWeightQuestionFormatter,
        InsightType.brand.name: BrandQuestionFormatter,
    }

    @classmethod
    def get(cls, insight_type: str):
        return cls.formatters.get(insight_type)

    @classmethod
    def get_available_types(cls) -> List[str]:
        return list(cls.formatters.keys())
