"""
Reader for semantic parsing on the DROP dataset.
"""

import logging
from typing import Dict, List
import os
import gzip
import tarfile
import json

from overrides import overrides

from allennlp.common import JsonDict
from allennlp.data.instance import Instance
from allennlp.data.fields import (Field, TextField, MetadataField, ProductionRuleField,
                                  ListField, IndexField, KnowledgeGraphField)
from allennlp.data.dataset_readers.dataset_reader import DatasetReader
from allennlp.data.tokenizers import WordTokenizer
from allennlp.data.tokenizers.tokenizer import Tokenizer
from allennlp.data.tokenizers.word_splitter import SpacyWordSplitter
from allennlp.data.token_indexers import SingleIdTokenIndexer
from allennlp.data.token_indexers.token_indexer import TokenIndexer
from allennlp.semparse.worlds.world import ParsingError

from semparse.context import ParagraphQuestionContext, DropWorld
from semparse.context import util as context_util

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@DatasetReader.register("drop-semantic-parser")
class DropDatasetReader(DatasetReader):
    """
    This ``DatasetReader`` takes DROP ``*.examples`` files and converts them into
    ``Instances`` suitable for use with the ``DropSemanticParser``.
    """
    def __init__(self,
                 lazy: bool = False,
                 tables_directory: str = None,
                 offline_logical_forms_directory: str = None,
                 max_offline_logical_forms: int = 100,
                 keep_if_no_logical_forms: bool = False,
                 tokenizer: Tokenizer = None,
                 question_token_indexers: Dict[str, TokenIndexer] = None,
                 table_token_indexers: Dict[str, TokenIndexer] = None,
                 use_table_for_vocab: bool = False,
                 max_table_tokens: int = None,
                 output_agendas: bool = False,
                 embedding_file_for_entity_extraction: str = None,
                 distance_threshold_for_entity_extraction: float = 0.3) -> None:
        super().__init__(lazy=lazy)
        self._tables_directory = tables_directory
        self._offline_logical_forms_directory = offline_logical_forms_directory
        self._max_offline_logical_forms = max_offline_logical_forms
        self._keep_if_no_logical_forms = keep_if_no_logical_forms
        self._tokenizer = tokenizer or WordTokenizer(SpacyWordSplitter(pos_tags=True))
        self._question_token_indexers = question_token_indexers or {"tokens": SingleIdTokenIndexer()}
        self._table_token_indexers = table_token_indexers or self._question_token_indexers
        self._use_table_for_vocab = use_table_for_vocab
        self._max_table_tokens = max_table_tokens
        self._output_agendas = output_agendas
        self._entity_extraction_embedding = None
        if embedding_file_for_entity_extraction:
            self._entity_extraction_embedding = context_util.read_pretrained_embedding(
                    embedding_file_for_entity_extraction)
        self._entity_extraction_distance_threshold = distance_threshold_for_entity_extraction

    @overrides
    def _read(self, file_path: str):
        # Checking if there is a single tarball with all the logical forms. If so, untaring it
        # first.
        if self._offline_logical_forms_directory:
            tarball_with_all_lfs: str = None
            for filename in os.listdir(self._offline_logical_forms_directory):
                if filename.endswith(".tar.gz"):
                    tarball_with_all_lfs = os.path.join(self._offline_logical_forms_directory,
                                                        filename)
                    break
            if tarball_with_all_lfs is not None:
                logger.info(f"Found a tarball in offline logical forms directory: {tarball_with_all_lfs}")
                logger.info("Assuming it contains logical forms for all questions and un-taring it.")
                # If you're running this with beaker, the input directory will be read-only and we
                # cannot untar the files in the directory itself. So we will do so in /tmp, but that
                # means the new offline logical forms directory will be /tmp.
                self._offline_logical_forms_directory = "/tmp/logical_forms/"
                tarfile.open(tarball_with_all_lfs,
                             mode='r:gz').extractall(path="/tmp/")
        tarball_with_all_tables: str = None
        for filename in os.listdir(self._tables_directory):
            if filename.endswith(".tar.gz"):
                tarball_with_all_tables = os.path.join(self._tables_directory,
                                                       filename)
                break
        if tarball_with_all_tables is not None:
            logger.info(f"Found a tarball in the tables directory: {tarball_with_all_tables}")
            logger.info("Assuming it contains tables for all questions and un-taring it.")
            # If you're running this with beaker, the input directory will be read-only and we
            # cannot untar the files in the directory itself. So we will do so in /tmp, but that
            # means the new tables directory will be /tmp.
            self._tables_directory = "/tmp/tables/"
            tarfile.open(tarball_with_all_tables,
                         mode='r:gz').extractall(path=self._tables_directory)
        with open(file_path, "r") as data_file:
            num_missing_logical_forms = 0
            num_qa_json = 0
            num_missing_tables = 0
            num_all_tables = 0
            num_instances = 0
            data = json.load(data_file)
            for passage_id, passage_data in data.items():
                num_all_tables += 1
                # We want the tagged file. The preprocessing script is expected to have saved it as
                # "<passage_id>.tagged".
                table_filename = os.path.join(self._tables_directory, f"{passage_id}.tagged")
                try:
                    table_lines = [line.split("\t") for line in open(table_filename).readlines()]
                    for qa_data in passage_data["qa_pairs"]:
                        num_qa_json += 1
                        question = qa_data["question"]
                        query_id = qa_data["query_id"]
                        if self._offline_logical_forms_directory:
                            logical_forms_filename = os.path.join(self._offline_logical_forms_directory,
                                                                  query_id + '.gz')
                            try:
                                logical_forms_file = gzip.open(logical_forms_filename)
                                logical_forms = []
                                for logical_form_line in logical_forms_file:
                                    logical_forms.append(logical_form_line.strip().decode('utf-8'))
                            except FileNotFoundError:
                                logger.debug(f'Missing search output for instance {query_id}; skipping...')
                                logical_forms = None
                                num_missing_logical_forms += 1
                                if not self._keep_if_no_logical_forms:
                                    continue
                        else:
                            logical_forms = None

                        instance = self.text_to_instance(question=question,
                                                         table_lines=table_lines,
                                                         answer_json=qa_data["answer"],
                                                         offline_search_output=logical_forms)
                        if instance is not None:
                            num_instances += 1
                            yield instance

                except FileNotFoundError:
                    logger.info(f"Missing table file: {table_filename}")
                    num_missing_tables += 1

        logger.info(f"Missing table files for {num_missing_tables} out of {num_all_tables} paragraphs")
        if self._offline_logical_forms_directory:
            logger.info(f"Missing lfs for {num_missing_logical_forms} out of {num_qa_json} instances")
            logger.info(f"Kept {num_instances} instances")

    def text_to_instance(self,  # type: ignore
                         question: str,
                         table_lines: List[List[str]],
                         answer_json: JsonDict,
                         offline_search_output: List[str] = None) -> Instance:
        """
        Reads text inputs and makes an instance. We assume we have access to DROP paragraphs parsed
        and tagged in a format similar to the tagged tables in WikiTableQuestions.
        # TODO(pradeep): Explain the format.

        Parameters
        ----------
        question : ``str``
            Input question
        table_lines : ``List[List[str]]``
            Preprocessed paragraph content. See ``ParagraphQuestionContext.read_from_lines``
            for the expected format.
        answer_json : ``JsonDict``
            The "answer" dict from the original data file.
        offline_search_output : List[str], optional
            List of logical forms, produced by offline search. Not required during test.
        """
        # pylint: disable=arguments-differ
        tokenized_question = self._tokenizer.tokenize(question.lower())
        question_field = TextField(tokenized_question, self._question_token_indexers)
        # TODO(pradeep): We'll need a better way to input processed lines.
        paragraph_context = ParagraphQuestionContext.read_from_lines(table_lines,
                                                                     tokenized_question,
                                                                     self._entity_extraction_embedding,
                                                                     self._entity_extraction_distance_threshold)
        target_values_field = MetadataField(answer_json)
        world = DropWorld(paragraph_context)
        world_field = MetadataField(world)
        # Note: Not passing any featre extractors when instantiating the field below. This will make
        # it use all the available extractors.
        table_field = KnowledgeGraphField(paragraph_context.get_knowledge_graph(),
                                          tokenized_question,
                                          self._table_token_indexers,
                                          tokenizer=self._tokenizer,
                                          include_in_vocab=self._use_table_for_vocab,
                                          max_table_tokens=self._max_table_tokens)
        production_rule_fields: List[Field] = []
        for production_rule in world.all_possible_actions():
            _, rule_right_side = production_rule.split(' -> ')
            is_global_rule = not world.is_instance_specific_entity(rule_right_side)
            field = ProductionRuleField(production_rule, is_global_rule=is_global_rule)
            production_rule_fields.append(field)
        action_field = ListField(production_rule_fields)

        fields = {'question': question_field,
                  'table': table_field,
                  'world': world_field,
                  'actions': action_field,
                  'target_values': target_values_field}

        # We'll make each target action sequence a List[IndexField], where the index is into
        # the action list we made above.  We need to ignore the type here because mypy doesn't
        # like `action.rule` - it's hard to tell mypy that the ListField is made up of
        # ProductionRuleFields.
        action_map = {action.rule: i for i, action in enumerate(action_field.field_list)}  # type: ignore
        if offline_search_output:
            action_sequence_fields: List[Field] = []
            for logical_form in offline_search_output:
                try:
                    expression = world.parse_logical_form(logical_form)
                except ParsingError as error:
                    logger.debug(f'Parsing error: {error.message}, skipping logical form')
                    logger.debug(f'Question was: {question}')
                    logger.debug(f'Logical form was: {logical_form}')
                    logger.debug(f'Table info was: {table_lines}')
                    continue
                except:
                    logger.error(logical_form)
                    raise
                action_sequence = world.get_action_sequence(expression)
                try:
                    index_fields: List[Field] = []
                    for production_rule in action_sequence:
                        index_fields.append(IndexField(action_map[production_rule], action_field))
                    action_sequence_fields.append(ListField(index_fields))
                except KeyError as error:
                    logger.debug(f'Missing production rule: {error.args}, skipping logical form')
                    logger.debug(f'Question was: {question}')
                    logger.debug(f'Table info was: {table_lines}')
                    logger.debug(f'Logical form was: {logical_form}')
                    continue
                if len(action_sequence_fields) >= self._max_offline_logical_forms:
                    break

            if not action_sequence_fields:
                # This is not great, but we're only doing it when we're passed logical form
                # supervision, so we're expecting labeled logical forms, but we can't actually
                # produce the logical forms.  We should skip this instance.  Note that this affects
                # _dev_ and _test_ instances, too, so your metrics could be over-estimates on the
                # full test data.
                return None
            fields['target_action_sequences'] = ListField(action_sequence_fields)
        if self._output_agendas:
            agenda_index_fields: List[Field] = []
            for agenda_string in world.get_agenda():
                agenda_index_fields.append(IndexField(action_map[agenda_string], action_field))
            if not agenda_index_fields:
                agenda_index_fields = [IndexField(-1, action_field)]
            fields['agenda'] = ListField(agenda_index_fields)
        return Instance(fields)
