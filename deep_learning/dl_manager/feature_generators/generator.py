##############################################################################
##############################################################################
# Imports
##############################################################################

from __future__ import annotations

import abc
import csv
import enum
import hashlib
import itertools
import json
import random
import typing
import warnings
import cProfile
import string

import nltk

from .util.text_cleaner import FormattingHandling, clean_issue_text
from .. import accelerator
from ..model_io import InputEncoding, classification8_lookup
from ..custom_kfold import stratified_trim
from .util import ontology
from ..config import conf
from ..logger import get_logger, timer
from ..database import DatabaseAPI
from ..data_manager import Dataset


log = get_logger('Base Feature Generator')

from ..data_manager_bootstrap import get_raw_text_file_name

csv.field_size_limit(100000000)

POS_CONVERSION = {
    "JJ": "a",
    "JJR": "a",
    "JJS": "a",
    "NN": "n",
    "NNS": "n",
    "NNP": "n",
    "NNPS": "n",
    "RB": "r",
    "RBR": "r",
    "RBS": "r",
    "VB": "v",
    "VBD": "v",
    "VBG": "v",
    "VBN": "v",
    "VBP": "v",
    "VBZ": "v",
    "WRB": "r",
}

ATTRIBUTE_CONSTANTS = {
    'n_attachments': 'n_attachments',
    'n_comments': 'n_comments',
    'len_comments': 'len_comments',
    'n_components': 'n_components',
    'len_description': 'len_description',
    'n_issuelinks': 'n_issuelinks',
    'n_labels': 'n_labels',
    'parent': 'parent',
    'n_subtasks': 'n_subtasks',
    'len_summary': 'len_summary',
    'n_votes': 'n_votes',
    'n_watches': 'n_watches',
    'issuetype': 'issuetype',
    'labels': 'labels',
    'priority': 'priority',
    'resolution': 'resolution',
    'status': 'status'
}

##############################################################################
##############################################################################
# Auxiliary Classes
##############################################################################


class FeatureEncoding(enum.Enum):
    Numerical = enum.auto()         # No metadata
    Categorical = enum.auto()       # No metadata
    Mixed = enum.auto()             # Metadata = Indices of categorical features
    Bert = enum.auto()              # No metadata 

    def as_string(self):
        match self:
            case self.Numerical:
                return 'numerical'
            case self.Categorical:
                return 'categorical'
            case self.Mixed:
                return 'mixed'
            case self.Bert:
                return 'bert'


    def from_string(self, x: str):
        match x:
            case 'numerical':
                return self.Numerical
            case 'categorical':
                return self.Categorical
            case 'mixed':
                return self.Mixed
            case 'bert':
                return self.Bert
            case _:
                raise ValueError(f'Invalid feature encoding: {x}')


def _escape(x):
    for ws in string.whitespace:
        x = x.replace(ws, '_')
    x = x.replace('.', 'dot')
    for illegal in '/<>:"/\\|?*\'':
        x = x.replace(illegal, '')
    return x


class _NullDict(dict):
    def __missing__(self, key):
        return key


class ParameterSpec(typing.NamedTuple):
    description: str
    type: str


##############################################################################
##############################################################################
# Main Class
##############################################################################


class AbstractFeatureGenerator(abc.ABC):

    def __init__(self, *,
                 pretrained_generator_settings: dict | None = None,
                 **params):
        self.__params = params
        self.__pretrained = pretrained_generator_settings
        self.__colors = None
        self.__keys=  None
        if self.__pretrained is not None:
            if self.__params:
                raise ValueError(
                    'Feature generator does not take params when pretrained settings are given'
                )
            # Populate params for default pre-processing,
            # which does not require any trained settings.
            for name in AbstractFeatureGenerator.get_parameters():
                if name in self.__pretrained:
                    self.__params[name] = self.__pretrained[name]
            if 'ontology-classes' in self.__pretrained:
                aux = conf.get('system.storage.auxiliary_map')
                conf.set('run.ontology-classes', aux[self.__pretrained['ontology-classes']])

    @property
    def params(self) -> dict[str, str]:
        return self.__params

    @property
    def pretrained(self) -> dict | None:
        return self.__pretrained

    @property
    def colors(self) -> list[int]:
        if self.__colors is None:
            raise RuntimeError('No colors yet')
        return self.__colors

    @property
    def issue_keys(self) -> list[str]:
        if self.__keys is None:
            raise RuntimeError('No keys yet')
        return self.__keys

    def save_pretrained(self, pretrained_settings: dict, auxiliary_files: list[str] = []):
        log.info(f'Saving {self.__class__.__name__} feature encoding')
        settings = '_'.join(
            f'{key}-{value}' for key, value in self.__params.items()
        )
        filename = f'{self.__class__.__name__}__{settings}'
        prefix = conf.get('system.storage.file-prefix')
        filename = f'{prefix}_{hashlib.sha512(filename.encode()).hexdigest()}.json'
        for name in AbstractFeatureGenerator.get_parameters():
            if name in self.__params:
                pretrained_settings[name] = self.__params[name]
        ontologies = conf.get('run.ontology-classes')
        if ontologies:
            pretrained_settings['ontology-classes'] = ontologies
            conf.get('system.storage.auxiliary').append(ontologies)
        conf.get('system.storage.generators').append(filename)
        conf.get('system.storage.auxiliary').extend(auxiliary_files)
        with open(filename, 'w') as file:
            json.dump(
                {
                    'settings': pretrained_settings,
                    'generator': self.__class__.__name__,
                },
                file
            )

    @staticmethod
    @abc.abstractmethod
    def input_encoding_type() -> InputEncoding:
        """Type of input encoding generated by this generator.
        """

    @abc.abstractmethod
    def generate_vectors(self,
                         tokenized_issues: list[list[str]],
                         metadata,
                         args: dict[str, str]):
        # TODO: implement this method
        # TODO: this method should take in data, and generate
        # TODO: the corresponding feature vectors
        pass

    @staticmethod
    @abc.abstractmethod
    def feature_encoding() -> FeatureEncoding:
        pass

    @staticmethod
    @abc.abstractmethod
    def get_parameters() -> dict[str, ParameterSpec]:
        return {
            'max-len': ParameterSpec(
                description='words limit of the issue text',
                type='int'
            ),
            'disable-lowercase': ParameterSpec(
                description='transform words to lowercase',
                type='bool'
            ),
            'disable-stopwords': ParameterSpec(
                description='remove stopwords from text',
                type='bool'
            ),
            'use-stemming': ParameterSpec(
                description='stem the words in the text',
                type='bool'
            ),
            'use-lemmatization': ParameterSpec(
                description='Use lemmatization on words in the text',
                type='bool'
            ),
            'use-pos': ParameterSpec(
                'Enhance words in the text with part of speech information',
                type='bool'
            ),
            'class-limit': ParameterSpec(
                description='limit the amount of items per class',
                type='int'
            ),
        }

    def load_data_from_db(self, query, metadata_attributes):
        api: DatabaseAPI = conf.get('system.storage.database-api')
        issue_ids = api.select_issues(query)
        labels = {
            'detection': [],
            'classification3': [],
            'classification3simplified': [],
            'classification8': [],
            'issue_keys': [],
            'issue_ids': issue_ids
        }
        classification_indices = {
            'Existence': [],
            'Property': [],
            'Executive': [],
            'Non-Architectural': []
        }
        if self.pretrained is None:
            raw_labels = api.get_labels(issue_ids)
            for index, raw in enumerate(raw_labels):
                self.update_labels(labels,
                                   classification_indices,
                                   index,
                                   raw['existence'],
                                   raw['executive'],
                                   raw['property'])
        attributes = ['summary', 'description', 'key'] + metadata_attributes
        raw_data = api.get_issue_data(issue_ids, attributes, raise_on_partial_result=True)
        warnings.warn('Replace code again once database wrapper has been fixed')
        # texts = [
        #     issue.pop('summary') + issue.pop('description') for issue in raw_data
        # ]
        texts = []
        for issue in raw_data:
            summary = x if (x := issue.pop('summary')) is not None else ''
            description = x if (x := issue.pop('description')) is not None else ''
            labels['issue_keys'].append(issue.pop('key'))
            texts.append((summary, description))
        metadata = raw_data     # summary and description have been popped
        return texts, metadata, labels, classification_indices

    def update_labels(self,
                      labels,
                      classification_indices,
                      current_index,
                      is_existence,
                      is_executive,
                      is_property):
        if self.__colors is None:
            self.__colors = []
        if is_executive:  # Executive
            labels['classification3simplified'].append((0, 1, 0, 0))
            classification_indices['Executive'].append(current_index)
            self.__colors.append(0)
        elif is_property:  # Property
            labels['classification3simplified'].append((0, 0, 1, 0))
            classification_indices['Property'].append(current_index)
            self.__colors.append(1)
        elif is_existence:  # Existence
            labels['classification3simplified'].append((1, 0, 0, 0))
            classification_indices['Existence'].append(current_index)
            self.__colors.append(2)
        else:  # Non-architectural
            labels['classification3simplified'].append((0, 0, 0, 1))
            classification_indices['Non-Architectural'].append(current_index)
            self.__colors.append(3)

        if is_executive or is_property or is_existence:
            labels['detection'].append(True)
        else:
            labels['detection'].append(False)

        key = (is_existence, is_executive, is_property)
        labels['classification8'].append(classification8_lookup[key])
        labels['classification3'].append(key)

    def generate_features(self,
                          query,
                          output_mode: str):
        """Generate features from the data in the given source file,
        and store the results in the given target file.
        """
        metadata_attributes = [
            attr for attr in self.__params.get('metadata-attributes', '').split(',') if attr
        ]
        for attr in metadata_attributes:
            if attr not in ATTRIBUTE_CONSTANTS:
                raise ValueError(f'Unknown metadata attribute: {attr}')

        texts, metadata, labels, classification_indices = self.load_data_from_db(
            query, metadata_attributes
        )

        limit = int(self.params.get('class-limit', -1))
        if limit != -1 and self.pretrained is None:     # Only execute if not pretrained
            random.seed(42)
            stratified_indices = []
            for issue_type in classification_indices.keys():
                project_labels = [label for index, label in enumerate([label.split('-')[0]
                                                                       for label in labels['issue_keys']])
                                  if index in classification_indices[issue_type]]
                trimmed_indices = stratified_trim(limit, project_labels)
                stratified_indices.extend([classification_indices[issue_type][idx] for idx in trimmed_indices])
            texts = [text for idx, text in enumerate(texts) if idx in stratified_indices]
            for key in labels.keys():
                labels[key] = [label for idx, label in enumerate(labels[key]) if idx in stratified_indices]

        if self.input_encoding_type() == InputEncoding.Text:
            tokenized_issues = [['. '.join(text)] for text in texts]
        else:
            #with cProfile.Profile() as p:
            #    tokenized_issues = self.preprocess(texts)
            #p.dump_stats('profile.txt')
            tokenized_issues = self.preprocess(texts)

        log.info('Generating feature vectors')
        with timer('Feature Generation'):
            output = self.generate_vectors(tokenized_issues, metadata, self.__params)
        output['labels'] = labels   # labels is empty when pretrained

        output['original'] = tokenized_issues
        if 'original' in output and not self.pretrained:    # Only dump original text when not pre-trained.
            with open(get_raw_text_file_name(), 'w') as file:
                mapping = {key: text
                           for key, text in zip(labels['issue_keys'], output['original'])}
                json.dump(mapping, file)
            del output['original']
        elif 'original' in output:
            del output['original']

        return Dataset(
            features=output['features'],
            labels=output['labels'][output_mode.lower()],
            shape=output['feature_shape'],
            embedding_weights=output.get('weights', None),
            vocab_size=output.get('vocab_size', None),
            weight_vector_length=output.get('word_vector_length', None),
            binary_labels=output['labels']['detection'],
            issue_keys=output['labels']['issue_keys'],
            ids=output['labels']['issue_ids']
        )

    def preprocess(self, issues):
        log.info('Preprocessing Features')
        with timer('Feature Preprocessing'):
            ontology_path = conf.get('run.ontology-classes')
            if ontology_path != '':
                ontology_table = ontology.load_ontology(ontology_path)
            else:
                ontology_table = None

            stopwords = nltk.corpus.stopwords.words('english')
            use_stemming = self.__params.get('use-stemming', 'False') == 'True'
            use_lemmatization = self.__params.get('use-lemmatization', 'False') == 'True'
            use_pos = self.__params.get('use-pos', 'False') == 'True'
            stemmer = nltk.stem.PorterStemmer()
            lemmatizer = nltk.stem.WordNetLemmatizer()
            use_lowercase = self.__params.get('disable-lowercase', 'False') == 'False'
            use_ontologies = conf.get('run.apply-ontology-classes')
            handling_string = self.__params.get('formatting-handling', 'markers')
            handling = FormattingHandling.from_string(handling_string)
            weights, tagdict, classes = nltk.load(
                'taggers/averaged_perceptron_tagger/averaged_perceptron_tagger.pickle'
            )
            tagger = accelerator.Tagger(weights, classes, tagdict)

            summaries, descriptions = (list(x) for x in zip(*issues))
            summaries = accelerator.bulk_clean_text_parallel(
                summaries, handling.as_string(), conf.get('system.resources.threads')
            )
            summaries = [clean_issue_text(summary) for summary in summaries]
            descriptions = accelerator.bulk_clean_text_parallel(
                descriptions, handling.as_string(), conf.get('system.resources.threads')
            )
            descriptions = [clean_issue_text(description) for description in descriptions]
            texts = [
                [
                    nltk.word_tokenize(sent.lower() if use_lowercase else sent)
                    for sent in itertools.chain(summary, description)
                ]
                for summary, description in zip(summaries, descriptions)
            ]
            tagged = tagger.bulk_tag_parallel(
                texts, conf.get('system.resources.threads')
            )
            tokenized_issues = []
            for issue in tagged:
                all_words = []

                # Tokenize
                for words in issue:
                     # Apply ontology simplification. Must be done before stemming/lemmatization
                    if use_ontologies:
                        #assert ontology_table is not None, 'Missing --ontology-classes'
                        words = ontology.apply_ontologies_to_sentence(words, ontology_table)

                    # Remove stopwords
                    if self.__params.get('disable-stopwords', 'False') != 'True':
                        words = [(word, tag) for word, tag in words if word not in stopwords]

                    if use_stemming and use_lemmatization:
                        raise ValueError('Cannot use both stemming and lemmatization')

                    if use_stemming:
                        words = [(stemmer.stem(word), tag) for word, tag in words]

                    if use_lemmatization:
                        words = [(lemmatizer.lemmatize(word, pos=POS_CONVERSION.get(tag, 'n')), tag)
                                 for word, tag in words]

                    if use_pos:
                        words = [f'{word}_{POS_CONVERSION.get(tag, tag)}' for word, tag in words]
                    else:
                        words = [word for word, _ in words]

                    # At this point, we forget about sentence order
                    all_words.extend(words)

                # Limit issue length
                if 'max-len' in self.__params:
                    if len(all_words) > int(self.__params['max-len']):
                        all_words = all_words[0:int(self.__params['max-len'])]

                tokenized_issues.append(all_words)

        log.info('Finished preprocessing')
        return tokenized_issues
