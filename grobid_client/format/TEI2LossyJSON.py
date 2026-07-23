"""
    Convert the rich, unambiguous, standard, generic, extendable TEI XML format of GROBID and Pub2TEI into 
    something similar to CORD-19 degraded JSON format (let's call it a working format)

    Original version: https://github.com/howisonlab/softcite-dataset/blob/master/code/corpus/TEI2LossyJSON.py
"""
from __future__ import annotations

import logging
import os
import uuid
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
import html
import re
from pathlib import Path
from typing import Any, Dict, Union, BinaryIO, Iterator

import dateparser
from bs4 import BeautifulSoup, Tag

# Configure module-level logger
logger: logging.Logger = logging.getLogger(__name__)
logger.propagate = False  # Prevent propagation to avoid duplicate logs

# Only configure basic logging if nothing is set up yet
if not logger.handlers and not logging.getLogger().handlers:
    # Basic configuration if not already configured by the application
    logging.basicConfig(level=logging.INFO)


class TEI2LossyJSONConverter:
    """Converter that can operate in two modes:
    - non-streaming (backwards-compatible): returns a full document dict for a single file
    - streaming: yields passages one by one to keep memory usage low when processing many files

    The class also provides utilities to process a directory of TEI files in parallel and in batches.
    """

    def __init__(self, validate_refs: bool = True) -> None:
        self.validate_refs: bool = validate_refs

    def convert_tei_file(self, tei_file: Union[str, Path, BinaryIO], stream: bool = False) -> Any:
        """Backward-compatible function. If stream=True returns a generator that yields passages (dicts).
        If stream=False returns the full document dict (same shape as original function).
        """
        # Load with BeautifulSoup but avoid building huge structures when streaming
        if hasattr(tei_file, 'read'):
            # File-like object (BinaryIO/StringIO)
            content = tei_file.read()
            if isinstance(content, bytes):
                content = content.decode('utf-8')
        else:
            # Path-like object
            with open(tei_file, 'r', encoding='utf-8') as f:
                content = f.read()
        soup = BeautifulSoup(content, 'xml')

        if soup.TEI is None:
            logger.warning("%s: The TEI file is not well-formed or empty. Skipping the file.", tei_file)
            return None if not stream else iter(())

        # Determine passage level early
        passage_level = "sentence" if len(soup.find_all("s")) > len(soup.find_all("p")) else "paragraph"

        if stream:
            # Use generator that yields passages as they are formatted
            return self._iter_passages_from_soup(soup, passage_level)
        else:
            # Build the full document (backward compatible)
            document = OrderedDict()
            document['level'] = passage_level

            biblio_structure = OrderedDict()
            document['biblio'] = biblio_structure

            text_structure = []
            document['body_text'] = text_structure
            figures_and_tables = []
            document['figures_and_tables'] = figures_and_tables
            references_structure = []
            document['references'] = references_structure

            # Populate header and body using the same traversal used by the generator
            for child in soup.TEI.children:
                if child.name == 'teiHeader':
                    # Header parsing mirrors original behavior
                    title_node = child.find("title", attrs={"type": "main", "level": "a"})
                    biblio_structure["title"] = title_node.text if title_node else ""
                    biblio_structure["authors"] = list(
                        filter(
                            lambda x: x.strip() != "",
                            [
                                " ".join(
                                    [
                                        author.find('forename').text if author.find('forename') is not None else "",
                                        author.find('surname').text if author.find('surname') is not None else ""
                                    ]
                                ) for author in child.find_all("author")
                            ]
                        )
                    )

                    doi_node = child.find("idno", type="DOI")
                    if doi_node:
                        biblio_structure['doi'] = doi_node.text

                    md5_node = child.find("idno", type="MD5")
                    if md5_node:
                        biblio_structure['hash'] = md5_node.text

                    pmc_idno = child.find("idno", type="PMC")
                    if pmc_idno:
                        biblio_structure['pmc'] = pmc_idno.text

                    pub_date = child.find("date", attrs={"type": "published"})
                    if pub_date:
                        iso_date = pub_date.attrs.get("when")
                        if iso_date:
                            biblio_structure["publication_date"] = iso_date
                            try:
                                year = dateparser.parse(iso_date).year
                                biblio_structure["publication_year"] = year
                            except Exception:
                                pass

                    publisherStmt = child.find("publicationStmt")
                    publisher_node = publisherStmt.find("publisher") if publisherStmt else None
                    if publisher_node:
                        biblio_structure["publisher"] = publisher_node.text

                    journal_node = child.find("title", attrs={"type": "main", "level": "j"})
                    if journal_node:
                        biblio_structure["journal"] = journal_node.text

                    journal_abbr_node = child.find("title", attrs={"type": "abbr", "level": "j"})
                    if journal_abbr_node:
                        biblio_structure["journal_abbr"] = journal_abbr_node.text

                    abstract_node = child.find("abstract")
                    if abstract_node:
                        abstract_paragraph_nodes = abstract_node.find_all("p")
                        if passage_level == "sentence":
                            biblio_structure["abstract"] = [
                                [
                                    {
                                        "id": sentence.get("xml:id") if sentence.has_attr("xml:id") else id,
                                        "text": sentence.text,
                                        "coords": [
                                            box_to_dict(coord.split(","))
                                            for coord in sentence['coords'].split(";")
                                        ] if sentence.has_attr("coords") else [],
                                        "refs": get_refs_with_offsets(sentence)
                                    }
                                    for id, sentence in enumerate(paragraph.find_all("s"))
                                ]
                                for paragraph in abstract_paragraph_nodes
                            ]
                        else:
                            biblio_structure["abstract"] = [
                                {
                                    "id": id,
                                    "text": paragraph.text,
                                    "coords": [
                                        box_to_dict(coord.split(","))
                                        for coord in paragraph['coords'].split(";")
                                    ] if paragraph.has_attr("coords") else [],
                                    "refs": get_refs_with_offsets(paragraph)
                                }
                                for id, paragraph in enumerate(abstract_paragraph_nodes)
                            ]

                elif child.name == 'text':
                    # Collect body_text using the generator to avoid duplicating logic
                    for passage in self._iter_passages_from_soup_for_text(child, passage_level):
                        text_structure.append(passage)

                    # Collect figures and tables (kept in memory as they should be relatively small)
                    figures_and_tables_xml = child.find_all("figure")
                    for item in figures_and_tables_xml:
                        item_id = item.attrs.get("xml:id") if item.has_attr("xml:id") else get_random_id()
                        desc = item.figDesc
                        head = item.head
                        label = item.label
                        if item.has_attr("type") and item.attrs["type"] == "table":
                            json_content = xml_table_to_json(item.table) if item.table else None
                            note = item.note
                            figures_and_tables.append(
                                {
                                    "id": item_id,
                                    "label": label.text if label else "",
                                    "head": head.text if head else "",
                                    "type": "table",
                                    "desc": desc.text if desc else "",
                                    "content": json_content,
                                    "note": note.text if note else "",
                                    "coords": [
                                        box_to_dict(coord.split(","))
                                        for coord in item['coords'].split(";")
                                    ] if item.has_attr("coords") else []
                                }
                            )
                        else:
                            graphic_coords = item.graphic.attrs['coords'] if item.graphic and item.graphic.has_attr(
                                "coords") else None
                            figures_and_tables.append(
                                {
                                    "id": item_id,
                                    "label": label.text if label else "",
                                    "head": head.text if head else "",
                                    "type": "figure",
                                    "desc": desc.text if desc else "",
                                    "note": item.note.text if item.note else "",
                                    "coords": [
                                        box_to_dict(coord.split(","))
                                        for coord in graphic_coords.split(";")
                                    ] if graphic_coords else []
                                }
                            )

                    # Extract references from listBibl with comprehensive processing
                    list_bibl = soup.find("listBibl")
                    if list_bibl:
                        for i, bibl_struct in enumerate(list_bibl.find_all("biblStruct"), 1):
                            ref_data = self._extract_comprehensive_reference_data(bibl_struct, i)
                            if ref_data:
                                references_structure.append(ref_data)

            return document

    def _extract_comprehensive_reference_data(self, bibl_struct: Tag, index: int) -> dict[str, Any] | None:
        """
        Extract detailed bibliographic information from TEI biblStruct elements.
        Implements comprehensive parsing for all standard TEI bibliographic components.
        """

        citation_data = OrderedDict()
        citation_data['id'] = f"b{index}"

        # Extract reference identifier if present
        xml_id = bibl_struct.get('{http://www.w3.org/XML/1998/namespace}id') or bibl_struct.get('xml:id')
        if xml_id:
            citation_data['target'] = xml_id

        # Initialize containers for different types of content
        contributor_list: list[dict[str, Any]] = []
        publication_metadata: dict[str, Any] = {}
        identifier_collection: dict[str, Any] = {}
        supplementary_info: list[str] = []
        link_references: list[str] = []

        # 1. Process analytic level information (article/conference paper content)
        analytic_section = bibl_struct.find("analytic")
        if analytic_section:
            # Extract title information from analytic level
            analytic_titles = analytic_section.find_all("title")
            for title_element in analytic_titles:
                title_level = title_element.get("level", "")
                title_content = self._clean_text(title_element.get_text())
                if title_content:
                    if title_level == "a":
                        citation_data['title'] = title_content
                    elif title_level == "j":
                        publication_metadata['journal'] = title_content

            # Extract author information from analytic level
            analytic_authors = analytic_section.find_all("author")
            for author_element in analytic_authors:
                author_info = self._extract_contributor_details(author_element)
                if author_info:
                    contributor_list.append(author_info)

            # Handle reference elements within analytic section
            analytic_ref = analytic_section.find("ref")
            if analytic_ref:
                ref_content = self._clean_text(analytic_ref.get_text())
                if ref_content:
                    citation_data['reference_text'] = ref_content
                if analytic_ref.get('target'):
                    citation_data['reference_uri'] = analytic_ref.get('target')

            # Process identifier elements in analytic section
            analytic_identifiers = analytic_section.find_all("idno")
            for identifier_element in analytic_identifiers:
                self._process_identifier_element(identifier_element, identifier_collection, 'analytic')

            # Process pointer elements in analytic section
            analytic_pointers = analytic_section.find_all("ptr")
            for pointer_element in analytic_pointers:
                self._process_pointer_element(pointer_element, link_references)

        # 2. Process monograph level information (book/journal publication details)
        monograph_section = bibl_struct.find("monogr")
        if monograph_section:
            # Extract title information from monograph level
            monograph_titles = monograph_section.find_all("title")
            for title_element in monograph_titles:
                title_level = title_element.get("level", "")
                title_content = self._clean_text(title_element.get_text())
                if title_content:
                    if title_level == "m" and not citation_data.get('title'):
                        citation_data['title'] = title_content  # Book title
                    elif title_level == "j" and not publication_metadata.get('journal'):
                        publication_metadata['journal'] = title_content
                    elif title_level == "s":
                        publication_metadata['series'] = title_content

            # Extract contributors from monograph level (authors/editors)
            monograph_contributors = monograph_section.find_all(["author", "editor"])
            for contributor_element in monograph_contributors:
                contributor_info = self._extract_contributor_details(contributor_element)
                if contributor_info:
                    if contributor_element.name == "editor":
                        contributor_info['role'] = 'editor'
                    contributor_list.append(contributor_info)

            # Extract imprint information (publication details)
            imprint_section = monograph_section.find("imprint")
            if imprint_section:
                self._process_imprint_details(imprint_section, publication_metadata)

            # Process identifier elements in monograph section
            monograph_identifiers = monograph_section.find_all("idno")
            for identifier_element in monograph_identifiers:
                self._process_identifier_element(identifier_element, identifier_collection, 'monograph')

            # Process pointer elements in monograph section
            monograph_pointers = monograph_section.find_all("ptr")
            for pointer_element in monograph_pointers:
                self._process_pointer_element(pointer_element, link_references)

        # 3. Process series level information
        series_section = bibl_struct.find("series")
        if series_section:
            series_titles = series_section.find_all("title")
            for title_element in series_titles:
                title_content = self._clean_text(title_element.get_text())
                if title_content and not publication_metadata.get('series'):
                    publication_metadata['series'] = title_content

            series_contributors = series_section.find_all(["author", "editor"])
            for contributor_element in series_contributors:
                contributor_info = self._extract_contributor_details(contributor_element)
                if contributor_info:
                    contributor_info['role'] = contributor_element.name
                    contributor_list.append(contributor_info)

        # 4. Process top-level identifiers within biblStruct
        top_level_identifiers = bibl_struct.find_all("idno")
        for identifier_element in top_level_identifiers:
            self._process_identifier_element(identifier_element, identifier_collection, 'biblstruct')

        # 5. Process notes and supplementary information
        note_elements = bibl_struct.find_all("note")
        for note_element in note_elements:
            note_content = self._clean_text(note_element.get_text())
            note_type = note_element.get("type", "")
            if note_content:
                if note_type == "raw_reference":
                    citation_data['raw_reference'] = note_content
                elif note_type:
                    citation_data[f'note_{note_type}'] = note_content
                else:
                    supplementary_info.append(note_content)

        # 6. Process pointer elements at biblStruct level
        biblstruct_pointers = bibl_struct.find_all("ptr")
        for pointer_element in biblstruct_pointers:
            self._process_pointer_element(pointer_element, link_references)

        # 7. Compile extracted information into final citation structure
        self._compile_citation_data(citation_data, contributor_list, publication_metadata,
                                   identifier_collection, supplementary_info, link_references)

        # Ensure we have meaningful content before returning
        if self._validate_citation_content(citation_data):
            return citation_data

        return None

    def _extract_contributor_details(self, contributor_element: Tag) -> dict[str, Any] | None:
        """Extract detailed information about authors, editors, and other contributors."""
        contributor_info: dict[str, Any] = {}

        # Extract name components
        surname_element = contributor_element.find("surname")
        forename_element = contributor_element.find("forename")

        if surname_element and forename_element:
            surname_text = self._clean_text(surname_element.get_text())
            forename_text = self._clean_text(forename_element.get_text())
            contributor_info['name'] = f"{forename_text} {surname_text}"
            contributor_info['surname'] = surname_text
            contributor_info['forename'] = forename_text
        elif surname_element:
            surname_text = self._clean_text(surname_element.get_text())
            contributor_info['name'] = surname_text
            contributor_info['surname'] = surname_text
        elif forename_element:
            forename_text = self._clean_text(forename_element.get_text())
            contributor_info['name'] = forename_text
            contributor_info['forename'] = forename_text
        else:
            # Fallback to full text content
            full_name = self._clean_text(contributor_element.get_text())
            if full_name:
                contributor_info['name'] = full_name

        # Extract affiliation information
        affiliation_element = contributor_element.find("affiliation")
        if affiliation_element:
            affiliation_text = self._clean_text(affiliation_element.get_text())
            if affiliation_text:
                contributor_info['affiliation'] = affiliation_text

        return contributor_info if contributor_info.get('name') else None

    def _process_identifier_element(self, identifier_element: Tag, identifier_collection: dict[str, Any], level: str) -> None:
        """Process identifier elements (DOI, ISBN, ISSN, etc.) and organize by type and level."""
        identifier_text = self._clean_text(identifier_element.get_text())
        identifier_type = identifier_element.get("type", "").lower()

        if identifier_text:
            # Create level-specific container if it doesn't exist
            level_key = f"{level}_identifiers"
            if level_key not in identifier_collection:
                identifier_collection[level_key] = {}

            # Store identifier by type
            if identifier_type:
                identifier_collection[level_key][identifier_type] = identifier_text
            else:
                identifier_collection[level_key]['unknown'] = identifier_text

    def _process_pointer_element(self, pointer_element: Tag, link_references: list[str]) -> None:
        """Process pointer elements that contain external links."""
        pointer_target = pointer_element.get("target", "").strip()
        if pointer_target:
            link_references.append(pointer_target)

    def _process_imprint_details(self, imprint_element: Tag, publication_metadata: dict[str, Any]) -> None:
        """Extract and process imprint information including publisher, dates, and page ranges."""

        # Extract publisher information
        publisher_elements = imprint_element.find_all("publisher")
        for publisher_element in publisher_elements:
            publisher_name = self._clean_text(publisher_element.get_text())
            if publisher_name:
                publication_metadata['publisher'] = publisher_name
                publisher_location = publisher_element.get("from")
                if publisher_location:
                    publication_metadata['publisher_location'] = publisher_location

        # Extract date information
        date_elements = imprint_element.find_all("date")
        for date_element in date_elements:
            date_type = date_element.get("type", "")
            date_content = self._clean_text(date_element.get_text())
            date_when = date_element.get("when")

            if date_when:
                publication_metadata['publication_date'] = date_when
                # Extract year from ISO date
                year_match = re.search(r'\b(19|20)\d{2}\b', date_when)
                if year_match:
                    publication_metadata['year'] = int(year_match.group())
            elif date_content:
                if date_type:
                    publication_metadata[f'date_{date_type}'] = date_content
                else:
                    publication_metadata['publication_date_text'] = date_content
                # Try to extract year from text
                year_match = re.search(r'\b(19|20)\d{2}\b', date_content)
                if year_match:
                    publication_metadata['year'] = int(year_match.group())

        # Extract bibliographic scope information (pages, volume, issue)
        scope_elements = imprint_element.find_all("biblScope")
        for scope_element in scope_elements:
            scope_unit = scope_element.get("unit", "")
            scope_text = self._clean_text(scope_element.get_text())
            scope_from = scope_element.get("from")
            scope_to = scope_element.get("to")

            if scope_unit == "page":
                if scope_from:
                    publication_metadata['page_start'] = scope_from
                if scope_to:
                    publication_metadata['page_end'] = scope_to
                if scope_text and not scope_from and not scope_to:
                    publication_metadata['pages'] = scope_text
            elif scope_unit in ["volume", "vol"]:
                publication_metadata['volume'] = scope_text
            elif scope_unit in ["issue", "num"]:
                publication_metadata['issue'] = scope_text
            elif scope_unit == "chapter":
                publication_metadata['chapter'] = scope_text

    def _compile_citation_data(self, citation_data: dict[str, Any], contributors: list[dict[str, Any]],
                              publication_metadata: dict[str, Any], identifiers: dict[str, Any],
                              supplementary_info: list[str], links: list[str]) -> None:
        """Compile all extracted information into the final citation structure."""
        # Process contributors
        if contributors:
            authors = [c for c in contributors if c.get('role') != 'editor']
            editors = [c for c in contributors if c.get('role') == 'editor']

            if authors:
                if len(authors) == 1:
                    citation_data['authors'] = authors[0]['name']
                else:
                    citation_data['authors'] = [author['name'] for author in authors]

            if editors:
                if len(editors) == 1:
                    citation_data['editors'] = editors[0]['name']
                else:
                    citation_data['editors'] = [editor['name'] for editor in editors]

        # Merge publication metadata
        for key, value in publication_metadata.items():
            if value:
                citation_data[key] = value

        # Merge identifier information
        for level, level_identifiers in identifiers.items():
            for id_type, id_value in level_identifiers.items():
                # Prioritize common identifier types at top level
                if id_type in ['doi', 'isbn', 'issn', 'pmc', 'pmid', 'arxiv']:
                    citation_data[id_type] = id_value
                else:
                    # Store other identifiers in nested structure
                    if 'identifiers' not in citation_data:
                        citation_data['identifiers'] = {}
                    citation_data['identifiers'][f"{level}_{id_type}"] = id_value

        # Add supplementary information
        if supplementary_info:
            if len(supplementary_info) == 1:
                citation_data['notes'] = supplementary_info[0]
            else:
                citation_data['notes'] = supplementary_info

        # Add link references
        if links:
            if len(links) == 1:
                citation_data['url'] = links[0]
            else:
                citation_data['urls'] = links

    def _validate_citation_content(self, citation_data: dict[str, Any]) -> bool:
        """Validate that the citation contains meaningful information."""
        # Check for essential bibliographic elements
        essential_elements = ['title', 'authors', 'journal', 'doi', 'isbn', 'issn', 'pmc', 'pmid']

        # Check if any essential element is present
        has_essential = any(citation_data.get(element) for element in essential_elements)

        # Check for fallback elements
        has_fallback = any(citation_data.get(element) for element in ['raw_reference', 'reference_text'])

        return has_essential or has_fallback

    def _extract_person_data(self, person_element: Tag) -> dict[str, Any] | None:
        """
        Extract person data (author/editor) from TEI persName or author elements.
        Handles various name formats and affiliations.
        """

        person_data: dict[str, Any] = {}

        # Try different name extraction methods
        forename = person_element.find("forename")
        surname = person_element.find("surname")

        if forename and surname:
            # Standard format: forename + surname
            forename_text = self._clean_text(forename.get_text())
            surname_text = self._clean_text(surname.get_text())
            person_data['name'] = f"{forename_text} {surname_text}"
            person_data['forename'] = forename_text
            person_data['surname'] = surname_text
        elif surname:
            # Surname only
            surname_text = self._clean_text(surname.get_text())
            person_data['name'] = surname_text
            person_data['surname'] = surname_text
        elif forename:
            # Forename only
            forename_text = self._clean_text(forename.get_text())
            person_data['name'] = forename_text
            person_data['forename'] = forename_text
        else:
            # Try to get name from full text content
            full_name = self._clean_text(person_element.get_text())
            if full_name:
                person_data['name'] = full_name
                # Try to parse into components
                name_parts = full_name.split()
                if len(name_parts) >= 2:
                    person_data['surname'] = name_parts[-1]
                    person_data['forename'] = " ".join(name_parts[:-1])

        # Extract affiliation if present
        affiliation = person_element.find("affiliation")
        if affiliation:
            aff_text = self._clean_text(affiliation.get_text())
            if aff_text:
                person_data['affiliation'] = aff_text

                # Try to extract institution and location
                # Look for common patterns like "Institution, City, Country"
                parts = [part.strip() for part in aff_text.split(',') if part.strip()]
                if len(parts) >= 1:
                    person_data['institution'] = parts[0]
                if len(parts) >= 2:
                    person_data['location'] = ", ".join(parts[1:])

        return person_data if person_data.get('name') else None

    def _clean_text(self, text: str) -> str:
        """
        Clean and normalize text content to handle encoding issues and extra whitespace.
        """
        if not text:
            return ""

        # Handle common encoding issues
        if isinstance(text, bytes):
            try:
                text = text.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    text = text.decode('latin-1')
                except UnicodeDecodeError:
                    text = text.decode('utf-8', errors='ignore')

        # Normalize whitespace and strip
        text = re.sub(r'\s+', ' ', text.strip())

        # Remove any potential XML/HTML entities
        text = html.unescape(text)

        return text

    def _iter_passages_from_soup(self, soup: BeautifulSoup, passage_level: str) -> Iterator[Dict[str, Union[str, Dict[str, str]]]]:
        """Yield formatted passages discovered in the TEI soup. This yields the same structures
        as get_formatted_passage but one at a time to keep memory usage low."""
        for child in soup.TEI.children:
            if child.name == 'text':
                for passage in self._iter_passages_from_soup_for_text(child, passage_level):
                    yield passage

    def _iter_passages_from_soup_for_text(self, text_node: Tag, passage_level: str) -> Iterator[Dict[str, Union[str, Dict[str, str]]]]:
        head_paragraph = None

        # Process body and back sections
        for section in text_node.find_all(['body', 'back']):
            # Only get direct child divs of this section (handle namespace variants)
            div_nodes = []
            for child in section.children:
                if hasattr(child, 'name') and child.name:
                    # Handle both namespaced and non-namespaced divs
                    if child.name == "div" or child.name.endswith(":div"):
                        div_nodes.append(child)

            for id_div, div in enumerate(div_nodes):
                # Skip references div as it's handled separately
                if div.get("type") == "references":
                    continue

                div_type = div.get("type")

                # Check if this is a header-only div (no content, no nested divs)
                # If so, capture its header as context for subsequent divs
                head = div.find("head")
                direct_p_nodes = [c for c in div.children if hasattr(c, 'name') and c.name == "p"]
                direct_formula_nodes = [c for c in div.children if hasattr(c, 'name') and c.name == "formula"]
                nested_divs = [c for c in div.children if hasattr(c, 'name') and (c.name == "div" or (c.name and c.name.endswith(":div")))]
                has_direct_content = len(direct_p_nodes) > 0 or len(direct_formula_nodes) > 0
                
                if head and not has_direct_content and len(nested_divs) == 0:
                    # This is a header-only div with no nested content
                    # Capture the header for the next div
                    head_paragraph = self._clean_text(head.get_text())
                    continue  # Skip to next div, the header will be used by subsequent sibling

                # Process this div and potentially nested divs
                for passage in self._process_div_with_nested_content(div, passage_level, head_paragraph):
                    yield passage
                
                # Reset head_paragraph after it's been used by a content-bearing div
                head_paragraph = None


    def _process_div_with_nested_content(self, div: Tag, passage_level: str, head_paragraph: str | None = None) -> Iterator[Dict[str, Union[str, Dict[str, str]]]]:
        """
        Process a div and its nested content, handling various back section types.
        Supports nested divs for complex back sections like annex with multiple subsections.
        Also handles formula elements that are direct children of divs.
        """
        head = div.find("head")
        p_nodes = div.find_all("p")
        head_section = None
        current_head_paragraph = None

        # Check if this div has nested divs first (handle namespace variants)
        nested_divs = []
        for child in div.children:
            if hasattr(child, 'name') and child.name:
                # Handle both namespaced and non-namespaced divs
                if child.name == "div" or child.name.endswith(":div"):
                    nested_divs.append(child)

        # Count only direct child paragraphs and formulas, not those in nested divs
        direct_p_nodes = [child for child in div.children if hasattr(child, 'name') and child.name == "p"]
        direct_formula_nodes = [child for child in div.children if hasattr(child, 'name') and child.name == "formula"]
        has_direct_content = len(direct_p_nodes) > 0 or len(direct_formula_nodes) > 0

        if len(nested_divs) > 0 and not has_direct_content:
            # This is a container div - process each nested div independently
            for nested_div in nested_divs:
                # Skip references divs
                if nested_div.get("type") == "references":
                    continue
                # Pass None as head_paragraph to ensure nested divs use their own headers
                for passage in self._process_div_with_nested_content(nested_div, passage_level, None):
                    yield passage
            return  # Don't process this div further

        # Determine the section header and content type for divs with content
        if head:
            if not has_direct_content:
                # This div has only a head, no paragraphs or formulas (standalone head)
                current_head_paragraph = self._clean_text(head.get_text())
            else:
                # This div has both head and content - head is the section header
                head_section = self._clean_text(head.get_text())
        else:
            # If no head element, try to use the type attribute as head_section
            div_type = div.get("type")
            if div_type:
                # Handle specific div types with appropriate section names
                if div_type == "acknowledgement":
                    head_section = "Acknowledgements"
                elif div_type == "conflict":
                    head_section = "Conflicts of Interest"
                elif div_type == "contribution":
                    head_section = "Author Contributions"
                elif div_type == "availability":
                    # Only set as default if this div has its own content
                    if has_direct_content:
                        head_section = "Data Availability"
                elif div_type == "annex":
                    head_section = "Annex"
                else:
                    # Generic handling - capitalize and format
                    head_section = div_type.replace("_", " ").title()

        # Process direct children (paragraphs and formulas) in document order
        for child in div.children:
            if not hasattr(child, 'name') or not child.name:
                continue

            if child.name == "p":
                paragraph_id = get_random_id(prefix="p_")

                if passage_level == "sentence":
                    for id_s, sentence in enumerate(child.find_all("s")):
                        struct = get_formatted_passage(current_head_paragraph or head_paragraph, head_section, paragraph_id, sentence)
                        if self.validate_refs:
                            for ref in struct['refs']:
                                assert ref['offset_start'] < ref['offset_end'], "Wrong offsets"
                                assert struct['text'][ref['offset_start']:ref['offset_end']] == ref['text'], "Cannot apply offsets"
                        yield struct
                else:
                    struct = get_formatted_passage(current_head_paragraph or head_paragraph, head_section, paragraph_id, child)
                    if self.validate_refs:
                        for ref in struct['refs']:
                            assert ref['offset_start'] < ref['offset_end'], "Wrong offsets"
                            assert struct['text'][ref['offset_start']:ref['offset_end']] == ref['text'], "Cannot apply offsets"
                    yield struct

            elif child.name == "formula":
                # Process formula elements as passages
                formula_id = get_random_id(prefix="f_")
                formula_text = self._clean_text(child.get_text())
                
                if formula_text:
                    # Create a passage structure for the formula
                    formula_passage = {
                        "id": formula_id,
                        "text": formula_text,
                        "coords": [
                            box_to_dict(coord.split(","))
                            for coord in child.get("coords", "").split(";")
                        ] if child.has_attr("coords") else [],
                        "refs": [],
                        "type": "formula"
                    }
                    
                    if current_head_paragraph or head_paragraph:
                        formula_passage["head_paragraph"] = current_head_paragraph or head_paragraph
                    if head_section:
                        formula_passage["head_section"] = head_section
                    
                    # Extract formula label if present
                    label = child.find("label")
                    if label:
                        formula_passage["label"] = self._clean_text(label.get_text())
                    
                    yield formula_passage

        # Update head_paragraph for potential next div
        if current_head_paragraph is not None:
            head_paragraph = current_head_paragraph

    def process_directory(self, directory: Union[str, Path], pattern: str = "*.tei.xml", parallel: bool = True, workers: int | None = None) -> Iterator[Dict]:
        """Process a directory of TEI files and yield converted documents.
        When parallel=True this uses ProcessPoolExecutor to parallelize file-level conversion.
        Each yielded item is a dict with keys: 'path' and 'document' (document may be None on parse error).
        """
        directory = Path(directory)
        files = list(directory.rglob(pattern))
        if not parallel or len(files) <= 1:
            for f in files:
                yield {"path": f, "document": self.convert_tei_file(f, stream=False)}
            return

        # Use processes for CPU-bound parsing when many files are available
        workers = workers or min(32, (os.cpu_count() or 1))
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_convert_file_worker, str(f)): f for f in files}
            for fut in as_completed(futures):
                f = futures[fut]
                try:
                    doc = fut.result()
                except Exception:
                    logger.exception("Error processing %s", f)
                    doc = None
                yield {"path": f, "document": doc}


def _convert_file_worker(path: str) -> Any:
    """Worker used by ProcessPoolExecutor. Imports inside function to avoid pickling issues."""
    from bs4 import BeautifulSoup
    # Reuse existing top-level helpers from this module by importing here
    with open(path, 'r') as f:
        content = f.read()
    soup = BeautifulSoup(content, 'xml')
    converter = TEI2LossyJSONConverter()
    return converter.convert_tei_file(path, stream=False)


def box_to_dict(coord_list: list[str]) -> dict[str, float]:
    """Convert coordinate list to dictionary format."""
    if len(coord_list) >= 4:
        return {
            "x": float(coord_list[0]),
            "y": float(coord_list[1]),
            "width": float(coord_list[2]),
            "height": float(coord_list[3])
        }
    return {}


def get_random_id(prefix: str = "") -> str:
    """Generate a random ID with optional prefix."""
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def get_refs_with_offsets(element: Tag) -> list[dict[str, Any]]:
    """Extract references with their text offsets from an element."""
    refs: list[dict[str, Any]] = []

    # Apply the same text cleaning as get_formatted_passage
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text.strip())
        text = html.unescape(text)
        return text

    # Now extract references with offsets based on the cleaned text
    def traverse_and_collect(node: Any, current_pos: int = 0) -> tuple[str, int]:
        """
        Recursively traverse the DOM tree, building cleaned text content and tracking exact positions.
        Returns tuple: (text_content, next_position)
        """
        if hasattr(node, 'name') and node.name:
            # This is an element node
            if node.name == "ref" and node.get("type") == "bibr":
                # Found a reference - get its cleaned text and record its exact position
                ref_text = _clean_text(node.get_text())
                if ref_text:  # Only record non-empty references
                    refs.append({
                        "type": node.get("type", ""),
                        "target": node.get("target", ""),
                        "text": ref_text,
                        "offset_start": current_pos,
                        "offset_end": current_pos + len(ref_text)
                    })
                # Return the cleaned reference text and advance position
                return ref_text, current_pos + len(ref_text)
            else:
                # Process children in document order and accumulate their cleaned text
                text_parts = []
                pos = current_pos
                for child in node.children:
                    child_text, new_pos = traverse_and_collect(child, pos)
                    if child_text is not None:
                        text_parts.append(child_text)
                    pos = new_pos
                return "".join(text_parts), pos
        else:
            # This is a text node (NavigableString) - be more careful with cleaning
            text_content = str(node)

            # For text nodes, we need to be more careful about whitespace
            # Only apply the full cleaning at the end for the complete text
            return text_content, current_pos + len(text_content)

    # Build raw text with accurate positions first
    raw_text, _ = traverse_and_collect(element, 0)

    # Now apply the same cleaning as get_formatted_passage to the complete text
    final_text = _clean_text(raw_text)

    # Adjust all reference offsets to match the cleaned text
    final_refs: list[dict[str, Any]] = []
    for ref in refs:
        # Find the reference text in the cleaned text to get correct offsets
        ref_text = ref['text']

        # The reference text was also cleaned, so we need to find it in the final cleaned text
        # We can search around the original position to find the correct occurrence
        search_start = max(0, ref['offset_start'] - 10)  # Look a bit before the original position
        search_end = min(len(final_text), ref['offset_end'] + 10)  # Look a bit after
        search_area = final_text[search_start:search_end]

        # Find the reference in the search area
        relative_pos = search_area.find(ref_text)
        if relative_pos != -1:
            final_start = search_start + relative_pos
            final_end = final_start + len(ref_text)

            final_refs.append({
                "type": ref["type"],
                "target": ref["target"],
                "text": ref_text,
                "offset_start": final_start,
                "offset_end": final_end
            })

    return final_refs


def get_formatted_passage(head_paragraph: str | None, head_section: str | None, paragraph_id: str, element: Tag) -> dict[str, Any]:
    """Format a passage (paragraph or sentence) with metadata and references."""
    # Import the clean_text method
    def _clean_text_local(text: str) -> str:
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text.strip())
        text = html.unescape(text)
        return text

    text = _clean_text_local(element.get_text())
    refs = get_refs_with_offsets(element)

    passage = {
        "id": paragraph_id,
        "text": text,
        "coords": [
            box_to_dict(coord.split(","))
            for coord in element.get("coords", "").split(";")
        ] if element.has_attr("coords") else [],
        "refs": refs
    }

    if head_paragraph:
        passage["head_paragraph"] = head_paragraph
    if head_section:
        passage["head_section"] = head_section

    return passage


def xml_table_to_markdown(table_element: Tag | None) -> str | None:
    """Convert XML table to markdown format."""
    if not table_element:
        return None
    
    markdown_lines = []
    
    # Process table rows
    for row in table_element.find_all("row"):
        cells = []
        for cell in row.find_all("cell"):
            cell_text = cell.get_text().strip()
            cells.append(cell_text)
        
        if cells:
            markdown_lines.append("| " + " | ".join(cells) + " |")
    
    return "\n".join(markdown_lines) if markdown_lines else None


def xml_table_to_json(table_element: Tag | None) -> dict[str, Any] | None:
    """Convert XML table to JSON format."""
    if not table_element:
        return None
    
    table_data = {
        "headers": [],
        "rows": [],
        "metadata": {}
    }
    
    # Check if table has a header row (thead)
    thead = table_element.find("thead")
    if thead:
        header_row = thead.find("row")
        if header_row:
            for cell in header_row.find_all("cell"):
                cell_text = cell.get_text().strip()
                table_data["headers"].append(cell_text)
    
    # Process table body rows
    tbody = table_element.find("tbody")
    if tbody:
        rows = tbody.find_all("row")
    else:
        # If no tbody, get all rows
        rows = table_element.find_all("row")
        # Skip first row if we already processed it as header
        if thead and rows:
            rows = rows[1:]
    
    for row in rows:
        row_data = []
        for cell in row.find_all("cell"):
            cell_text = cell.get_text().strip()
            row_data.append(cell_text)
        
        if row_data:
            table_data["rows"].append(row_data)
    
    # Add metadata
    table_data["metadata"] = {
        "row_count": len(table_data["rows"]),
        "column_count": len(table_data["headers"]) if table_data["headers"] else (len(table_data["rows"][0]) if table_data["rows"] else 0),
        "has_headers": len(table_data["headers"]) > 0
    }
    
    return table_data if table_data["rows"] else None


# Backwards compatible top-level function that uses the class
def convert_tei_file(tei_file: Union[str, Path, BinaryIO], stream: bool = False) -> Any:
    converter = TEI2LossyJSONConverter()
    return converter.convert_tei_file(tei_file, stream=stream)
