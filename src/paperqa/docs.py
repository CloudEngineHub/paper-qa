from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import urllib.request
import warnings
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

from aviary.core import Message
from lmi import Embeddable, EmbeddingModel, LLMModel
from lmi.types import set_llm_session_ids
from lmi.utils import gather_with_concurrency
from pydantic import BaseModel, ConfigDict, Field

from paperqa.clients import DEFAULT_CLIENTS, DocMetadataClient
from paperqa.core import llm_parse_json, map_fxn_summary
from paperqa.llms import (
    NumpyVectorStore,
    VectorStore,
)
from paperqa.prompts import CANNOT_ANSWER_PHRASE
from paperqa.readers import read_doc
from paperqa.settings import MaybeSettings, get_settings
from paperqa.types import Context, Doc, DocDetails, DocKey, PQASession, Text
from paperqa.utils import (
    citation_to_docname,
    get_loop,
    maybe_is_html,
    maybe_is_pdf,
    maybe_is_text,
    md5sum,
)

logger = logging.getLogger(__name__)


class Docs(BaseModel):  # noqa: PLW1641  # TODO: add __hash__
    """A collection of documents to be used for answering questions."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    docs: dict[DocKey, Doc | DocDetails] = Field(default_factory=dict)
    texts: list[Text] = Field(default_factory=list)
    docnames: set[str] = Field(default_factory=set)
    texts_index: VectorStore = Field(default_factory=NumpyVectorStore)
    name: str = Field(default="default", description="Name of this docs collection")
    deleted_dockeys: set[DocKey] = Field(default_factory=set)

    def __eq__(self, other) -> bool:
        if (
            not isinstance(other, type(self))
            or not isinstance(self.texts_index, NumpyVectorStore)
            or not isinstance(other.texts_index, NumpyVectorStore)
        ):
            return NotImplemented
        return (
            self.docs == other.docs
            and len(self.texts) == len(other.texts)
            and all(
                self_text == other_text
                for self_text, other_text in zip(self.texts, other.texts, strict=True)
            )
            and self.docnames == other.docnames
            and self.texts_index == other.texts_index
            and self.name == other.name
            # NOTE: ignoring deleted_dockeys
        )

    def clear_docs(self) -> None:
        self.texts = []
        self.docs = {}
        self.docnames = set()
        self.texts_index.clear()

    def _get_unique_name(self, docname: str) -> str:
        """Create a unique name given proposed name."""
        suffix = ""
        while docname + suffix in self.docnames:
            # move suffix to next letter
            suffix = "a" if not suffix else chr(ord(suffix) + 1)
        docname += suffix
        return docname

    def add_file(
        self,
        file: BinaryIO,
        citation: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
        settings: MaybeSettings = None,
        llm_model: LLMModel | None = None,
        embedding_model: EmbeddingModel | None = None,
    ) -> str | None:
        warnings.warn(
            "The synchronous `add_file` method is being deprecated in favor of the"
            " asynchronous `aadd_file` method, this deprecation will conclude in"
            " version 6.",
            category=DeprecationWarning,
            stacklevel=2,
        )
        return get_loop().run_until_complete(
            self.aadd_file(
                file,
                citation=citation,
                docname=docname,
                dockey=dockey,
                settings=settings,
                llm_model=llm_model,
                embedding_model=embedding_model,
            )
        )

    async def aadd_file(
        self,
        file: BinaryIO,
        citation: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
        title: str | None = None,
        doi: str | None = None,
        authors: list[str] | None = None,
        settings: MaybeSettings = None,
        llm_model: LLMModel | None = None,
        embedding_model: EmbeddingModel | None = None,
        **kwargs,
    ) -> str | None:
        """Add a document to the collection."""
        # just put in temp file and use existing method
        suffix = ".txt"
        if maybe_is_pdf(file):
            suffix = ".pdf"
        elif maybe_is_html(file):
            suffix = ".html"

        with tempfile.NamedTemporaryFile(suffix=suffix) as f:
            f.write(file.read())
            f.seek(0)
            return await self.aadd(
                Path(f.name),
                citation=citation,
                docname=docname,
                dockey=dockey,
                title=title,
                doi=doi,
                authors=authors,
                settings=settings,
                llm_model=llm_model,
                embedding_model=embedding_model,
                **kwargs,
            )

    def add_url(
        self,
        url: str,
        citation: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
        settings: MaybeSettings = None,
        llm_model: LLMModel | None = None,
        embedding_model: EmbeddingModel | None = None,
    ) -> str | None:
        warnings.warn(
            "The synchronous `add_url` method is being deprecated in favor of the"
            " asynchronous `aadd_url` method, this deprecation will conclude in"
            " version 6.",
            category=DeprecationWarning,
            stacklevel=2,
        )
        return get_loop().run_until_complete(
            self.aadd_url(
                url,
                citation=citation,
                docname=docname,
                dockey=dockey,
                settings=settings,
                llm_model=llm_model,
                embedding_model=embedding_model,
            )
        )

    async def aadd_url(
        self,
        url: str,
        citation: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
        settings: MaybeSettings = None,
        llm_model: LLMModel | None = None,
        embedding_model: EmbeddingModel | None = None,
    ) -> str | None:
        """Add a document to the collection."""
        with urllib.request.urlopen(url) as f:  # noqa: ASYNC210, S310
            # need to wrap to enable seek
            file = BytesIO(f.read())
            return await self.aadd_file(
                file,
                citation=citation,
                docname=docname,
                dockey=dockey,
                settings=settings,
                llm_model=llm_model,
                embedding_model=embedding_model,
            )

    def add(
        self,
        path: str | os.PathLike,
        citation: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
        title: str | None = None,
        doi: str | None = None,
        authors: list[str] | None = None,
        settings: MaybeSettings = None,
        llm_model: LLMModel | None = None,
        embedding_model: EmbeddingModel | None = None,
        **kwargs,
    ) -> str | None:
        warnings.warn(
            "The synchronous `add` method is being deprecated in favor of the"
            " asynchronous `aadd` method, this deprecation will conclude in"
            " version 6.",
            category=DeprecationWarning,
            stacklevel=2,
        )
        return get_loop().run_until_complete(
            self.aadd(
                path,
                citation=citation,
                docname=docname,
                dockey=dockey,
                title=title,
                doi=doi,
                authors=authors,
                settings=settings,
                llm_model=llm_model,
                embedding_model=embedding_model,
                **kwargs,
            )
        )

    async def aadd(  # noqa: PLR0912
        self,
        path: str | os.PathLike,
        citation: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
        title: str | None = None,
        doi: str | None = None,
        authors: list[str] | None = None,
        settings: MaybeSettings = None,
        llm_model: LLMModel | None = None,
        embedding_model: EmbeddingModel | None = None,
        **kwargs,
    ) -> str | None:
        """Add a document to the collection."""
        all_settings = get_settings(settings)
        parse_config = all_settings.parsing
        dockey_is_content_hash = False
        if dockey is None:
            # md5 sum of file contents (not path!)
            dockey = md5sum(path)
            dockey_is_content_hash = True
        if llm_model is None:
            llm_model = all_settings.get_llm()
        if citation is None:
            # Peek first chunk
            texts = await read_doc(
                path,
                Doc(docname="", citation="", dockey=dockey),  # Fake doc
                chunk_chars=parse_config.chunk_size,
                overlap=parse_config.overlap,
                page_size_limit=parse_config.page_size_limit,
                use_block_parsing=parse_config.pdfs_use_block_parsing,
                parse_pdf=parse_config.parse_pdf,
            )
            if not texts or not texts[0].text.strip():
                raise ValueError(f"Could not read document {path}. Is it empty?")
            result = await llm_model.call_single(
                messages=[
                    Message(
                        content=parse_config.citation_prompt.format(text=texts[0].text)
                    ),
                ],
            )
            citation = cast("str", result.text)
            if (
                len(citation) < 3  # noqa: PLR2004
                or "Unknown" in citation
                or "insufficient" in citation
            ):
                citation = f"Unknown, {os.path.basename(path)}, {datetime.now().year}"

        doc = Doc(
            docname=self._get_unique_name(
                citation_to_docname(citation) if docname is None else docname
            ),
            citation=citation,
            dockey=dockey,
        )

        # try to extract DOI / title from the citation
        if (doi is title is None) and parse_config.use_doc_details:
            # TODO: specify a JSON schema here when many LLM providers support this
            messages = [
                Message(
                    content=parse_config.structured_citation_prompt.format(
                        citation=citation
                    ),
                ),
            ]
            result = await llm_model.call_single(
                messages=messages,
            )
            # This code below tries to isolate the JSON
            # based on observed messages from LLMs
            # it does so by isolating the content between
            # the first { and last } in the response.
            # Since the anticipated structure should  not be nested,
            # we don't have to worry about nested curlies.
            clean_text = cast("str", result.text).split("{", 1)[-1].split("}", 1)[0]
            clean_text = "{" + clean_text + "}"
            try:
                citation_json = json.loads(clean_text)
                if citation_title := citation_json.get("title"):
                    title = citation_title
                if citation_doi := citation_json.get("doi"):
                    doi = citation_doi
                if citation_author := citation_json.get("authors"):
                    authors = citation_author
            except (json.JSONDecodeError, AttributeError):
                # json.JSONDecodeError: clean_text was not actually JSON
                # AttributeError: citation_json was not a dict (e.g. a list)
                logger.warning(
                    "Failed to parse all of title, DOI, and authors from the"
                    " ParsingSettings.structured_citation_prompt's response"
                    f" {clean_text}, consider using a manifest file or specifying a"
                    " different citation prompt."
                )
        # see if we can upgrade to DocDetails
        # if not, we can progress with a normal Doc
        # if "fields_to_overwrite_from_metadata" is used:
        # will map "docname" to "key", and "dockey" to "doc_id"
        if (title or doi) and parse_config.use_doc_details:
            if kwargs.get("metadata_client"):
                metadata_client = kwargs["metadata_client"]
            else:
                metadata_client = DocMetadataClient(
                    session=kwargs.pop("session", None),
                    clients=kwargs.pop("clients", DEFAULT_CLIENTS),
                )

            query_kwargs: dict[str, Any] = {}

            if doi:
                query_kwargs["doi"] = doi
            if authors:
                query_kwargs["authors"] = authors
            if title:
                query_kwargs["title"] = title
            if dockey_is_content_hash:
                # if we had an autogenerated dockey, we would like our
                # metadata to be able to overwrite it if possible
                doc.fields_to_overwrite_from_metadata = {
                    d
                    for d in doc.fields_to_overwrite_from_metadata
                    if d not in {"dockey", "doc_id"}
                }

            doc = await metadata_client.upgrade_doc_to_doc_details(
                doc, **(query_kwargs | kwargs)
            )

        texts = await read_doc(
            path,
            doc,
            chunk_chars=parse_config.chunk_size,
            overlap=parse_config.overlap,
            page_size_limit=parse_config.page_size_limit,
            use_block_parsing=parse_config.pdfs_use_block_parsing,
            parse_pdf=parse_config.parse_pdf,
        )
        # loose check to see if document was loaded
        if (
            not texts
            or len(texts[0].text) < 10  # noqa: PLR2004
            or (
                not parse_config.disable_doc_valid_check
                # Use the first few text chunks to avoid potential issues with
                # title page parsing in the first chunk
                and not maybe_is_text("".join(text.text for text in texts[:5]))
            )
        ):
            raise ValueError(
                f"This does not look like a text document: {path}. Pass disable_check"
                " to ignore this error."
            )
        if await self.aadd_texts(texts, doc, all_settings, embedding_model):
            return doc.docname
        return None

    def add_texts(
        self,
        texts: list[Text],
        doc: Doc,
        settings: MaybeSettings = None,
        embedding_model: EmbeddingModel | None = None,
    ) -> bool:
        warnings.warn(
            "The synchronous `add_texts` method is being deprecated in favor of the"
            " asynchronous `aadd_texts` method, this deprecation will conclude in"
            " version 6.",
            category=DeprecationWarning,
            stacklevel=2,
        )
        return get_loop().run_until_complete(
            self.aadd_texts(
                texts, doc, settings=settings, embedding_model=embedding_model
            )
        )

    async def aadd_texts(
        self,
        texts: list[Text],
        doc: Doc,
        settings: MaybeSettings = None,
        embedding_model: EmbeddingModel | None = None,
    ) -> bool:
        """
        Add chunked texts to the collection.

        This is useful to use if you have already chunked the texts yourself.

        Returns:
            True if the doc was added, otherwise False if already in the collection.
        """
        if doc.dockey in self.docs:
            return False
        if not texts:
            raise ValueError("No texts to add.")

        all_settings = get_settings(settings)
        if not all_settings.parsing.defer_embedding and not embedding_model:
            # want to embed now!
            embedding_model = all_settings.get_embedding_model()

        # 0. Short-circuit if it is caught by a filter
        for doc_filter in all_settings.parsing.doc_filters or []:
            if not doc.matches_filter_criteria(doc_filter):
                return False

        # 1. Calculate text embeddings if not already present
        if embedding_model and texts[0].embedding is None:
            for t, t_embedding in zip(
                texts,
                await embedding_model.embed_documents(texts=[t.text for t in texts]),
                strict=True,
            ):
                t.embedding = t_embedding
        # 2. Update texts' and Doc's name
        if doc.docname in self.docnames:
            new_docname = self._get_unique_name(doc.docname)
            for t in texts:
                t.name = t.name.replace(doc.docname, new_docname)
            doc.docname = new_docname
        # 3. Update self
        # NOTE: we defer adding texts to the texts index to retrieval time
        # (e.g. `self.texts_index.add_texts_and_embeddings(texts)`)
        if doc.docname and doc.dockey:
            self.docs[doc.dockey] = doc
            self.texts += texts
            self.docnames.add(doc.docname)
            return True
        return False

    def delete(
        self,
        name: str | None = None,
        docname: str | None = None,
        dockey: DocKey | None = None,
    ) -> None:
        """Delete a document from the collection."""
        # name is an alias for docname
        name = docname if name is None else name

        if name is not None:
            doc = next((doc for doc in self.docs.values() if doc.docname == name), None)
            if doc is None:
                return
            if doc.docname and doc.dockey:
                self.docnames.remove(doc.docname)
                dockey = doc.dockey
        del self.docs[dockey]
        self.deleted_dockeys.add(dockey)
        self.texts = list(filter(lambda x: x.doc.dockey != dockey, self.texts))

    async def _build_texts_index(self, embedding_model: EmbeddingModel) -> None:
        texts = [t for t in self.texts if t not in self.texts_index]
        # For any embeddings we are supposed to lazily embed, embed them now
        to_embed = [t for t in texts if t.embedding is None]
        if to_embed:
            for t, t_embedding in zip(
                to_embed,
                await embedding_model.embed_documents(texts=[t.text for t in to_embed]),
                strict=True,
            ):
                t.embedding = t_embedding
        await self.texts_index.add_texts_and_embeddings(texts)

    async def retrieve_texts(
        self,
        query: str,
        k: int,
        settings: MaybeSettings = None,
        embedding_model: EmbeddingModel | None = None,
        partitioning_fn: Callable[[Embeddable], int] | None = None,
    ) -> list[Text]:
        """Perform MMR search with the input query on the internal index."""
        settings = get_settings(settings)
        if embedding_model is None:
            embedding_model = settings.get_embedding_model()

        # TODO: should probably happen elsewhere
        self.texts_index.mmr_lambda = settings.texts_index_mmr_lambda

        await self._build_texts_index(embedding_model)
        _k = k + len(self.deleted_dockeys)
        matches: list[Text] = cast(
            "list[Text]",
            (
                await self.texts_index.max_marginal_relevance_search(
                    query,
                    k=_k,
                    fetch_k=2 * _k,
                    embedding_model=embedding_model,
                    partitioning_fn=partitioning_fn,
                )
            )[0],
        )
        matches = [m for m in matches if m.doc.dockey not in self.deleted_dockeys]
        return matches[:k]

    def get_evidence(
        self,
        query: PQASession | str,
        exclude_text_filter: set[str] | None = None,
        settings: MaybeSettings = None,
        callbacks: Sequence[Callable] | None = None,
        embedding_model: EmbeddingModel | None = None,
        summary_llm_model: LLMModel | None = None,
        partitioning_fn: Callable[[Embeddable], int] | None = None,
    ) -> PQASession:
        warnings.warn(
            "The synchronous `get_evidence` method is being deprecated in favor of the"
            " asynchronous `aget_evidence` method, this deprecation will conclude in"
            " version 6.",
            category=DeprecationWarning,
            stacklevel=2,
        )
        return get_loop().run_until_complete(
            self.aget_evidence(
                query=query,
                exclude_text_filter=exclude_text_filter,
                settings=settings,
                callbacks=callbacks,
                embedding_model=embedding_model,
                summary_llm_model=summary_llm_model,
                partitioning_fn=partitioning_fn,
            )
        )

    async def aget_evidence(
        self,
        query: PQASession | str,
        exclude_text_filter: set[str] | None = None,
        settings: MaybeSettings = None,
        callbacks: Sequence[Callable] | None = None,
        embedding_model: EmbeddingModel | None = None,
        summary_llm_model: LLMModel | None = None,
        partitioning_fn: Callable[[Embeddable], int] | None = None,
    ) -> PQASession:

        evidence_settings = get_settings(settings)
        answer_config = evidence_settings.answer
        prompt_config = evidence_settings.prompts

        session = (
            PQASession(question=query, config_md5=evidence_settings.md5)
            if isinstance(query, str)
            else query
        )

        if not self.docs and len(self.texts_index) == 0:
            return session

        if embedding_model is None:
            embedding_model = evidence_settings.get_embedding_model()

        if summary_llm_model is None:
            summary_llm_model = evidence_settings.get_summary_llm()

        if exclude_text_filter is not None:
            text_name = Text.__name__
            warnings.warn(
                (
                    "The 'exclude_text_filter' argument did not work as intended"
                    f" due to a mix-up in excluding {text_name}.name vs {text_name}."
                    f" This bug enabled us to have 2+ contexts per {text_name}, so to"
                    " first-class that capability and simplify our implementation,"
                    " we're removing the 'exclude_text_filter' argument."
                    " This deprecation will conclude in version 6"
                ),
                category=DeprecationWarning,
                stacklevel=2,
            )

        if answer_config.evidence_retrieval:
            matches = await self.retrieve_texts(
                session.question,
                answer_config.evidence_k,
                evidence_settings,
                embedding_model,
                partitioning_fn=partitioning_fn,
            )
        else:
            matches = self.texts

        matches = (
            matches[: answer_config.evidence_k]
            if answer_config.evidence_retrieval
            else matches
        )

        prompt_templates = None
        if not answer_config.evidence_skip_summary:
            if prompt_config.use_json:
                prompt_templates = (
                    prompt_config.summary_json,
                    prompt_config.summary_json_system,
                )
            else:
                prompt_templates = (
                    prompt_config.summary,
                    prompt_config.system,
                )

        with set_llm_session_ids(session.id):
            results = await gather_with_concurrency(
                answer_config.max_concurrent_requests,
                [
                    map_fxn_summary(
                        text=m,
                        question=session.question,
                        summary_llm_model=summary_llm_model,
                        prompt_templates=prompt_templates,
                        extra_prompt_data={
                            "summary_length": answer_config.evidence_summary_length,
                            "citation": f"{m.name}: {m.doc.formatted_citation}",
                        },
                        parser=llm_parse_json if prompt_config.use_json else None,
                        callbacks=callbacks,
                        skip_citation_strip=answer_config.skip_evidence_citation_strip,
                    )
                    for m in matches
                ],
            )

        for _, llm_result in results:
            session.add_tokens(llm_result)

        session.contexts += [r for r, _ in results]
        return session

    def query(
        self,
        query: PQASession | str,
        settings: MaybeSettings = None,
        callbacks: Sequence[Callable] | None = None,
        llm_model: LLMModel | None = None,
        summary_llm_model: LLMModel | None = None,
        embedding_model: EmbeddingModel | None = None,
        partitioning_fn: Callable[[Embeddable], int] | None = None,
    ) -> PQASession:
        warnings.warn(
            "The synchronous `query` method is being deprecated in favor of the"
            " asynchronous `aquery` method, this deprecation will conclude in"
            " version 6.",
            category=DeprecationWarning,
            stacklevel=2,
        )
        return get_loop().run_until_complete(
            self.aquery(
                query,
                settings=settings,
                callbacks=callbacks,
                llm_model=llm_model,
                summary_llm_model=summary_llm_model,
                embedding_model=embedding_model,
                partitioning_fn=partitioning_fn,
            )
        )

    async def aquery(  # noqa: PLR0912
        self,
        query: PQASession | str,
        settings: MaybeSettings = None,
        callbacks: Sequence[Callable] | None = None,
        llm_model: LLMModel | None = None,
        summary_llm_model: LLMModel | None = None,
        embedding_model: EmbeddingModel | None = None,
        partitioning_fn: Callable[[Embeddable], int] | None = None,
    ) -> PQASession:
        query_settings = get_settings(settings)
        answer_config = query_settings.answer
        prompt_config = query_settings.prompts

        if llm_model is None:
            llm_model = query_settings.get_llm()
        if summary_llm_model is None:
            summary_llm_model = query_settings.get_summary_llm()
        if embedding_model is None:
            embedding_model = query_settings.get_embedding_model()

        session = (
            PQASession(question=query, config_md5=query_settings.md5)
            if isinstance(query, str)
            else query
        )
        contexts = session.contexts
        if answer_config.get_evidence_if_no_contexts and not contexts:
            session = await self.aget_evidence(
                session,
                callbacks=callbacks,
                settings=settings,
                embedding_model=embedding_model,
                summary_llm_model=summary_llm_model,
                partitioning_fn=partitioning_fn,
            )
            contexts = session.contexts
        pre_str = None
        if prompt_config.pre is not None:
            with set_llm_session_ids(session.id):
                messages = [
                    Message(role="system", content=prompt_config.system),
                    Message(
                        role="user",
                        content=prompt_config.pre.format(question=session.question),
                    ),
                ]
                pre = await llm_model.call_single(
                    messages=messages,
                    callbacks=callbacks,
                    name="pre",
                )
            session.add_tokens(pre)
            pre_str = pre.text

        # sort by first score, then name
        filtered_contexts = sorted(
            contexts,
            key=lambda x: (-x.score, x.text.name),
        )[: answer_config.answer_max_sources]
        # remove any contexts with a score below the cutoff
        filtered_contexts = [
            c
            for c in filtered_contexts
            if c.score >= answer_config.evidence_relevance_score_cutoff
        ]

        # shim deprecated flag
        # TODO: remove in v6
        context_inner_prompt = prompt_config.context_inner
        if (
            not answer_config.evidence_detailed_citations
            and "\nFrom {citation}" in context_inner_prompt
        ):
            # Only keep "\nFrom {citation}" if we are showing detailed citations
            context_inner_prompt = context_inner_prompt.replace("\nFrom {citation}", "")

        context_str_body = ""
        if answer_config.group_contexts_by_question:
            contexts_by_question: dict[str, list[Context]] = defaultdict(list)
            for c in filtered_contexts:
                # Fallback to the main session question if not available.
                # question attribute is optional, so if a user
                # sets contexts externally, it may not have a question.
                question = getattr(c, "question", session.question)
                contexts_by_question[question].append(c)

            context_sections = []
            for question, contexts_in_group in contexts_by_question.items():
                inner_strs = [
                    context_inner_prompt.format(
                        name=c.id,
                        text=c.context,
                        citation=c.text.doc.formatted_citation,
                        **(c.model_extra or {}),
                    )
                    for c in contexts_in_group
                ]
                # Create a section with a question heading
                section_header = f'Contexts related to the question: "{question}"'
                section = f"{section_header}\n\n" + "\n\n".join(inner_strs)
                context_sections.append(section)
            context_str_body = "\n\n----\n\n".join(context_sections)
        else:
            inner_context_strs = [
                context_inner_prompt.format(
                    name=c.id,
                    text=c.context,
                    citation=c.text.doc.formatted_citation,
                    **(c.model_extra or {}),
                )
                for c in filtered_contexts
            ]
            context_str_body = "\n\n".join(inner_context_strs)

        if pre_str:
            context_str_body += f"\n\nExtra background information: {pre_str}"

        context_str = prompt_config.context_outer.format(
            context_str=context_str_body,
            valid_keys=", ".join([c.id for c in filtered_contexts]),
        )

        if len(context_str_body.strip()) < 10:  # noqa: PLR2004
            answer_text = (
                f"{CANNOT_ANSWER_PHRASE} this question due to insufficient information."
            )
            answer_reasoning = None
        else:
            with set_llm_session_ids(session.id):
                prior_answer_prompt = ""
                if prompt_config.answer_iteration_prompt and session.answer:
                    prior_answer_prompt = prompt_config.answer_iteration_prompt.format(
                        prior_answer=session.answer
                    )
                messages = [
                    Message(role="system", content=prompt_config.system),
                    Message(
                        role="user",
                        content=prompt_config.qa.format(
                            context=context_str,
                            answer_length=answer_config.answer_length,
                            question=session.question,
                            example_citation=prompt_config.EXAMPLE_CITATION,
                            prior_answer_prompt=prior_answer_prompt,
                        ),
                    ),
                ]
                answer_result = await llm_model.call_single(
                    messages=messages,
                    callbacks=callbacks,
                    name="answer",
                )
            answer_text = cast("str", answer_result.text)
            answer_reasoning = answer_result.reasoning_content
            session.add_tokens(answer_result)
        # it still happens
        if (ex_citation := prompt_config.EXAMPLE_CITATION) in answer_text:
            answer_text = answer_text.replace(ex_citation, "")

        if answer_config.answer_filter_extra_background:
            answer_text = re.sub(
                r"\([Ee]xtra [Bb]ackground [Ii]nformation\)",  # spellchecker: disable-line
                "",
                answer_text,
            )

        if prompt_config.post is not None:
            with set_llm_session_ids(session.id):
                messages = [
                    Message(role="system", content=prompt_config.system),
                    Message(
                        role="user",
                        content=prompt_config.post.format(question=session.question),
                    ),
                ]
                post = await llm_model.call_single(
                    messages=messages,
                    callbacks=callbacks,
                    name="post",
                )
            answer_text = cast("str", post.text)
            answer_reasoning = post.reasoning_content
            session.add_tokens(post)
            answer_text = f"{answer_text}\n\n{post.text}"

        # now at end we modify, so we could have retried earlier
        session.raw_answer = answer_text
        session.answer_reasoning = answer_reasoning
        session.contexts = contexts
        session.context = context_str

        session.populate_formatted_answers_and_bib_from_raw_answer()

        return session
