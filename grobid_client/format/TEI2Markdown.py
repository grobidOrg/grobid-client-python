"""
Convert TEI XML format to Markdown format

This module provides functionality to convert GROBID TEI XML output to a clean
Markdown format with the following sections:
- Title
- Authors
- Affiliations  
- Publication date
- Fulltext
- Annex
- References
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Dict, Union, Optional, BinaryIO
from bs4 import BeautifulSoup, NavigableString, Tag
import logging
import dateparser

# Configure module-level logger
logger = logging.getLogger(__name__)
if not logger.handlers:
    # Basic configuration if not already configured by the application
    logging.basicConfig(level=logging.INFO)


class TEI2MarkdownConverter:
    """Converter that converts TEI XML to Markdown format."""

    def __init__(self) -> None:
        pass

    def convert_tei_file(self, tei_file: Union[str, Path, BinaryIO]) -> Optional[str]:
        """Convert a TEI file to Markdown format.
        
        Args:
            tei_file: Path to TEI file or file-like object
            
        Returns:
            Markdown content as string, or None if conversion fails
        """
        try:
            # Load with BeautifulSoup
            if isinstance(tei_file, (str, Path)):
                with open(tei_file, 'r', encoding='utf-8') as f:
                    content = f.read()
            else:
                content = tei_file.read()
                if isinstance(content, bytes):
                    content = content.decode('utf-8')
                
            soup = BeautifulSoup(content, 'xml')

            if soup.TEI is None:
                logger.warning("The TEI file is not well-formed or empty. Skipping the file.")
                return None

            markdown_sections = []
            
            # Extract title
            title = self._extract_title(soup)
            if title:
                markdown_sections.append(f"# {title}\n")

            # Extract authors
            authors = self._extract_authors(soup)
            if authors:
                for author in authors:
                    markdown_sections.append(f"{author}\n")
                markdown_sections.append("\n")

            # Extract affiliations
            affiliations = self._extract_affiliations(soup)
            if affiliations:
                affiliations_as_text = ", ".join(affiliations)
                markdown_sections.append(f"{affiliations_as_text}\n\n")

            # Extract publication date
            pub_date = self._extract_publication_date(soup)
            if pub_date:
                markdown_sections.append(f"Published on {pub_date}\n\n")

            # Extract abstract
            abstract = self._extract_abstract(soup)
            if abstract:
                markdown_sections.append(abstract)
                markdown_sections.append("\n\n")

            # Extract fulltext
            fulltext = self._extract_fulltext(soup)
            if fulltext:
                markdown_sections.append(fulltext)
                markdown_sections.append("\n")

            # Extract annex (acknowledgements, competing interests, etc.)
            annex = self._extract_annex(soup)
            if annex:
                markdown_sections.append(annex)
                markdown_sections.append("\n")

            # Extract references
            references = self._extract_references(soup)
            if references:
                markdown_sections.append("## References\n")
                markdown_sections.append(references)
                markdown_sections.append("\n")
            
            return "".join(markdown_sections)
            
        except Exception as e:
            logger.error(f"Error converting TEI to Markdown: {str(e)}")
            return None

    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract document title from TEI."""
        title_node = soup.find("title", attrs={"type": "main", "level": "a"})
        if title_node:
            return title_node.get_text().strip()
        return None

    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        """Extract authors from TEI document header (excluding references)."""
        authors = []

        # Only look in teiHeader to avoid picking up authors from references
        tei_header = soup.find("teiHeader")
        if not tei_header:
            return authors

        for author in tei_header.find_all("author"):
            forename = author.find('forename')
            surname = author.find('surname')

            if forename and surname:
                author_name = f"{forename.get_text().strip()} {surname.get_text().strip()}"
            elif surname:
                author_name = surname.get_text().strip()
            elif forename:
                author_name = forename.get_text().strip()
            else:
                continue

            if author_name.strip():
                authors.append(author_name.strip())

        return authors

    def _extract_affiliations(self, soup: BeautifulSoup) -> List[str]:
        """Extract affiliations from TEI document header (excluding references)."""
        affiliations = []

        # Only look in teiHeader to avoid picking up affiliations from references
        tei_header = soup.find("teiHeader")
        if not tei_header:
            return affiliations

        for affiliation in tei_header.find_all("affiliation"):
            # Get the full affiliation text
            affiliation_text = affiliation.get_text().strip()
            if affiliation_text:
                affiliations.append(affiliation_text)

        return affiliations

    def _extract_publication_date(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract publication date from TEI."""
        pub_date = soup.find("date", attrs={"type": "published"})
        if pub_date:
            iso_date = pub_date.attrs.get("when")
            if iso_date:
                try:
                    parsed_date = dateparser.parse(iso_date)
                    if parsed_date:
                        return parsed_date.strftime("%B %d, %Y")
                except Exception:
                    pass
                return iso_date
        return None

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        """Extract abstract from TEI."""
        abstract_paragraphs = []

        # Find abstract element
        abstract = soup.find("abstract")
        if not abstract:
            return ""

        # Extract paragraphs from abstract
        for p in abstract.find_all("p"):
            paragraph_text = self._process_paragraph(p)
            # Filter out empty paragraphs and standalone periods
            if paragraph_text.strip() and paragraph_text.strip() != ".":
                # Remove trailing periods that might create standalone lines
                cleaned_text = paragraph_text.strip()
                abstract_paragraphs.append(cleaned_text)

        return "\n\n".join(abstract_paragraphs)

    def _extract_fulltext(self, soup: BeautifulSoup) -> str:
        """Extract main body text from TEI."""
        fulltext_sections = []
        
        # Find body element
        body = soup.find("body")
        if not body:
            return ""
        
        # Process each div in the body
        for div in body.find_all("div"):
            # Get section heading
            head = div.find("head")
            if head:
                section_title = head.get_text().strip()
                if section_title:
                    fulltext_sections.append(f"### {section_title}\n")

            # Process direct children of the div in document order
            # This captures paragraphs, formulas, and other elements as they appear
            for child in div.children:
                if not hasattr(child, 'name') or not child.name:
                    continue
                    
                if child.name == "p":
                    paragraph_text = self._process_paragraph(child)
                    if paragraph_text.strip():
                        fulltext_sections.append(f"{paragraph_text}\n\n")
                elif child.name == "formula":
                    # Handle formula elements - extract text and optional label
                    formula_text = self._process_formula(child)
                    if formula_text.strip():
                        fulltext_sections.append(f"{formula_text}\n\n")
        
        return "".join(fulltext_sections)

    def _extract_annex(self, soup: BeautifulSoup) -> str:
        """Extract annex content (everything in <back> except references and content that should be in body) from TEI."""
        annex_sections = []

        # Find back element
        back = soup.find("back")
        if not back:
            return ""

        # Get all content from back (not just divs) - stream everything
        for child in back.children:
            if hasattr(child, 'name') and child.name:
                if child.name == "div":
                    # Skip the references div since it's handled separately
                    if child.get("type") == "references":
                        continue

                    # Skip methods-like content that should be in body, not annex
                    div_type = child.get("type", "").lower()
                    if div_type in ["methods", "results", "discussion", "introduction"]:
                        continue

                    # Process this div and any nested divs
                    self._process_div_and_nested_divs(child, annex_sections)
                elif child.name == "p":
                    # Direct paragraphs in back
                    paragraph_text = self._process_paragraph(child)
                    if paragraph_text.strip():
                        annex_sections.append(f"{paragraph_text}\n\n")
                # Add other elements as needed (e.g., notes, etc.)
                elif child.name not in ["listBibl"]:  # Skip listBibl, handled in references
                    # Get text content from other elements
                    text_content = child.get_text().strip()
                    if text_content:
                        annex_sections.append(f"{text_content}\n\n")

        return "".join(annex_sections)

    def _process_div_and_nested_divs(self, div: Tag, annex_sections: list[str]) -> None:
        """Process a div element and its nested div elements."""
        # Add section header if present for this div (avoid duplicates)
        head = div.find("head")
        if head and head.get_text().strip():
            header_text = f"### {head.get_text().strip()}\n\n"
            # Check if this header already exists to avoid duplication
            if header_text not in annex_sections:
                annex_sections.append(header_text)

        # Process direct children of this div in document order
        # This captures paragraphs, formulas, and other elements as they appear
        for child in div.children:
            if not hasattr(child, 'name') or not child.name:
                continue
                
            if child.name == "p":
                paragraph_text = self._process_paragraph(child)
                if paragraph_text.strip():
                    annex_sections.append(f"{paragraph_text}\n\n")
            elif child.name == "formula":
                # Handle formula elements
                formula_text = self._process_formula(child)
                if formula_text.strip():
                    annex_sections.append(f"{formula_text}\n\n")
            elif child.name == "div":
                # Process nested div elements
                self._process_div_and_nested_divs(child, annex_sections)

    def _extract_references(self, soup: BeautifulSoup) -> str:
        """Extract bibliographic references from TEI."""
        references = []

        # Find back element
        back = soup.find("back")
        if not back:
            return ""

        # Find the specific div with type="references"
        references_div = back.find("div", attrs={"type": "references"})
        if not references_div:
            return ""

        # Find listBibl element within the references div
        list_bibl = references_div.find("listBibl")
        if not list_bibl:
            return ""

        # Process each biblStruct
        for i, bibl_struct in enumerate(list_bibl.find_all("biblStruct"), 1):
            ref_text = self._format_reference(bibl_struct, i)
            if ref_text:
                references.append(ref_text)

        return "\n".join(references)

    def _process_paragraph(self, p_element: Tag) -> str:
        """Process a paragraph element and convert to markdown."""
        text_parts = []
        
        for element in p_element.children:
            if isinstance(element, NavigableString):
                text_parts.append(str(element))
            elif element.name == "ref":
                # Handle references - keep the text but don't add special formatting
                ref_text = element.get_text()
                text_parts.append(ref_text)
            elif element.name == "figure":
                # Handle figures
                fig_desc = element.find("figDesc")
                if fig_desc:
                    text_parts.append(f"\n*Figure: {fig_desc.get_text().strip()}*\n")
            elif element.name == "table":
                # Handle tables - convert to simple markdown
                table_md = self._table_to_markdown(element)
                if table_md:
                    text_parts.append(f"\n{table_md}\n")
            else:
                # For other elements, just get the text
                text_parts.append(element.get_text())
        
        return "".join(text_parts).strip()

    def _process_formula(self, formula_element: Tag) -> str:
        """Process a formula element and convert to markdown.
        
        Formulas are rendered as italicized text with optional equation label.
        """
        # Get the main formula text (excluding the label)
        formula_text_parts = []
        label_text = ""
        
        for child in formula_element.children:
            if hasattr(child, 'name') and child.name == "label":
                # Extract equation label (e.g., "(1)", "(2)")
                label_text = child.get_text().strip()
            elif isinstance(child, NavigableString):
                formula_text_parts.append(str(child))
            else:
                # Other elements within formula - get their text
                formula_text_parts.append(child.get_text())
        
        formula_text = "".join(formula_text_parts).strip()
        
        if formula_text:
            # Format as: *formula text* (label) if label exists
            if label_text:
                return f"*{formula_text}* {label_text}"
            return f"*{formula_text}*"
        return ""

    def _table_to_markdown(self, table_element: Tag) -> str:
        """Convert a table element to simple markdown."""
        markdown_lines = []
        
        # Process table rows
        for row in table_element.find_all("row"):
            cells = []
            for cell in row.find_all("cell"):
                cell_text = cell.get_text().strip()
                cells.append(cell_text)
            
            if cells:
                markdown_lines.append("| " + " | ".join(cells) + " |")
        
        return "\n".join(markdown_lines) if markdown_lines else ""

    def _format_reference(self, bibl_struct: Tag, ref_num: int) -> str:
        """
        Format a bibliographic reference with comprehensive TEI element handling.

        This method processes all standard TEI bibliographic elements including:
        - Title extraction from analytic and monogr levels
        - Author information from all levels with proper name formatting
        - Publication details (journal, year, volume, issue, pages)
        - Identifiers (DOI, PMID, PMCID, ISBN, ISSN)
        - URLs and external links from ptr elements
        - Raw reference fallback for unstructured data
        """
        reference_components = []

        # Reference identifier always comes first
        reference_components.append(f"**[{ref_num}]**")

        # Extract bibliographic information in hierarchical order
        ref_data = self._extract_bibliographic_data(bibl_struct)

        # Add title if available
        if ref_data.get('title'):
            reference_components.append(ref_data['title'])

        # Add authors with proper formatting
        if ref_data.get('authors'):
            author_text = self._format_authors(ref_data['authors'])
            reference_components.append(f"*{author_text}*")

        # Add publication venue (journal, book, etc.)
        if ref_data.get('venue'):
            reference_components.append(f"*{ref_data['venue']}*")

        # Add publication details
        publication_details = self._build_publication_details(ref_data)
        if publication_details:
            reference_components.append(publication_details)

        # Add identifiers and links
        identifiers_and_links = self._build_identifiers_and_links(ref_data)
        reference_components.extend(identifiers_and_links)

        # Fallback to raw reference if no structured data
        if len(reference_components) == 1:  # Only has reference number
            raw_reference = self._extract_raw_reference(bibl_struct)
            if raw_reference:
                reference_components.append(raw_reference)

        # Assemble final reference
        formatted_reference = " ".join(reference_components)

        # Ensure proper ending punctuation
        if not formatted_reference.endswith('.'):
            formatted_reference += "."

        return formatted_reference

    def _extract_bibliographic_data(self, bibl_struct: Tag) -> dict[str, Any]:
        """
        Extract comprehensive bibliographic data from TEI structure.

        Handles both analytic (article-level) and monogr (journal/book-level) information
        following standard TEI bibliographic structure.
        """
        bib_data = {
            'title': None,
            'authors': [],
            'venue': None,
            'year': None,
            'volume': None,
            'issue': None,
            'pages': None,
            'identifiers': {},
            'urls': [],
            'raw_text': None
        }

        # Process analytic section (article-level information)
        analytic = bibl_struct.find("analytic")
        if analytic:
            self._process_analytic_section(analytic, bib_data)

        # Process monogr section (journal/book-level information)
        monogr = bibl_struct.find("monogr")
        if monogr:
            self._process_monograph_section(monogr, bib_data)

        # Process series information if present
        series = bibl_struct.find("series")
        if series:
            self._process_series_section(series, bib_data)

        # Extract identifiers from all levels
        self._extract_identifiers(bibl_struct, bib_data)

        # Extract URLs and links
        self._extract_urls(bibl_struct, bib_data)

        return bib_data

    def _process_analytic_section(self, analytic: Tag, bib_data: dict[str, Any]) -> None:
        """Process the analytic section containing article-level information."""
        # Extract article title
        title = analytic.find("title", level="a")
        if title and title.get_text().strip():
            bib_data['title'] = title.get_text().strip()

        # Extract authors from analytic section
        for author in analytic.find_all("author"):
            author_info = self._extract_author_info(author)
            if author_info:
                bib_data['authors'].append(author_info)

    def _process_monograph_section(self, monogr: Tag, bib_data: dict[str, Any]) -> None:
        """Process the monograph section containing publication-level information."""
        # Extract title if no analytic title was found
        if not bib_data['title']:
            title = monogr.find("title")
            if title and title.get_text().strip():
                bib_data['title'] = title.get_text().strip()

        # Extract journal/book title
        journal = monogr.find("title", level="j")
        if journal and journal.get_text().strip():
            bib_data['venue'] = journal.get_text().strip()

        # Extract authors from monograph if no analytic authors
        if not bib_data['authors']:
            for author in monogr.find_all("author"):
                author_info = self._extract_author_info(author)
                if author_info:
                    bib_data['authors'].append(author_info)

        # Process imprint section containing publication details
        imprint = monogr.find("imprint")
        if imprint:
            self._process_imprint_section(imprint, bib_data)

    def _process_series_section(self, series: Tag, bib_data: dict[str, Any]) -> None:
        """Process series information for multi-part publications."""
        series_title = series.find("title", level="s")
        if series_title and series_title.get_text().strip():
            if bib_data['venue']:
                bib_data['venue'] += f" ({series_title.get_text().strip()})"
            else:
                bib_data['venue'] = series_title.get_text().strip()

    def _process_imprint_section(self, imprint: Tag, bib_data: dict[str, Any]) -> None:
        """Process the imprint section containing publication details."""
        # Extract publication date
        date = imprint.find("date")
        if date:
            bib_data['year'] = self._extract_year(date.get_text().strip())

        # Extract publication details from biblScope elements
        for bibl_scope in imprint.find_all("biblScope"):
            unit = bibl_scope.get("unit", "").lower()
            text = bibl_scope.get_text().strip()

            if unit in ["vol", "volume"] and text:
                bib_data['volume'] = text
            elif unit == "issue" and text:
                bib_data['issue'] = text
            elif unit == "page" and text:
                # Handle page ranges
                from_val = bibl_scope.get("from")
                to_val = bibl_scope.get("to")
                if from_val and to_val:
                    # Both from and to in same element
                    bib_data['pages'] = f"{from_val}-{to_val}"
                elif from_val:
                    # Only from specified, may get combined with another element
                    bib_data['pages'] = f"{from_val}-"
                elif to_val and bib_data.get('pages'):
                    # Only to specified, append to existing from
                    bib_data['pages'] = bib_data['pages'] + to_val
                elif text and not bib_data.get('pages'):
                    # Plain text, no from/to attributes
                    bib_data['pages'] = text

    def _extract_author_info(self, author: Tag) -> dict[str, str] | None:
        """Extract author information from a TEI author element."""
        author_info = {}

        # Handle persName wrapper
        pers_name = author.find("persName")
        if pers_name:
            forename = pers_name.find('forename')
            surname = pers_name.find('surname')
        else:
            forename = author.find('forename')
            surname = author.find('surname')

        # Extract name components
        if forename:
            author_info['forename'] = forename.get_text().strip()
        if surname:
            author_info['surname'] = surname.get_text().strip()

        return author_info if author_info else None

    def _extract_identifiers(self, bibl_struct: Tag, bib_data: dict[str, Any]) -> None:
        """Extract various identifier types from the bibliographic structure."""
        identifier_sections = [bibl_struct]

        # Add analytic and monogr sections if they exist
        analytic = bibl_struct.find("analytic")
        if analytic:
            identifier_sections.append(analytic)

        monogr = bibl_struct.find("monogr")
        if monogr:
            identifier_sections.append(monogr)

        # Extract identifiers from all sections
        for section in identifier_sections:
            if section:
                idnos = section.find_all("idno")
                for idno in idnos:
                    id_type = idno.get("type", "").lower()
                    id_value = idno.get_text().strip()

                    if id_type and id_value:
                        bib_data['identifiers'][id_type] = id_value

    def _extract_urls(self, bibl_struct: Tag, bib_data: dict[str, Any]) -> None:
        """Extract URLs and external links from ptr elements."""
        url_sections = [bibl_struct]

        # Add analytic and monogr sections if they exist
        analytic = bibl_struct.find("analytic")
        if analytic:
            url_sections.append(analytic)

        monogr = bibl_struct.find("monogr")
        if monogr:
            url_sections.append(monogr)

        # Extract URLs from all sections
        for section in url_sections:
            if section:
                ptrs = section.find_all("ptr")
                for ptr in ptrs:
                    target = ptr.get("target")
                    if target and target.strip():
                        bib_data['urls'].append(target.strip())

    def _extract_year(self, date_text: str) -> str:
        """Extract year from date text, handling various formats."""
        # Look for 4-digit year patterns
        year_match = re.search(r'\b(19|20)\d{2}\b', date_text)
        if year_match:
            return year_match.group()

        # Fallback to returning the original text
        return date_text.strip()

    def _format_authors(self, authors: list[dict[str, str]]) -> str:
        """Format author list for display."""
        formatted_authors = []

        for author in authors:
            if 'forename' in author and 'surname' in author:
                formatted_authors.append(f"{author['forename']} {author['surname']}")
            elif 'surname' in author:
                formatted_authors.append(author['surname'])
            elif 'forename' in author:
                formatted_authors.append(author['forename'])

        if not formatted_authors:
            return ""

        if len(formatted_authors) == 1:
            return formatted_authors[0]
        elif len(formatted_authors) == 2:
            return f"{formatted_authors[0]} and {formatted_authors[1]}"
        else:
            return f"{formatted_authors[0]} et al."

    def _build_publication_details(self, ref_data: dict[str, Any]) -> str:
        """Build publication details string from extracted data."""
        details = []

        if ref_data.get('year'):
            details.append(f"({ref_data['year']})")

        if ref_data.get('volume'):
            details.append(ref_data['volume'])

        if ref_data.get('issue'):
            details.append(f"({ref_data['issue']})")

        if ref_data.get('pages'):
            details.append(f"pp. {ref_data['pages']}")

        return " ".join(details)

    def _build_identifiers_and_links(self, ref_data: dict[str, Any]) -> list[str]:
        """Build list of formatted identifiers and links."""
        identifiers_and_links = []

        # Format DOI if present
        if 'doi' in ref_data['identifiers']:
            doi = ref_data['identifiers']['doi']
            identifiers_and_links.append(f"https://doi.org/{doi}")

        # Format other identifiers
        for id_type, id_value in ref_data['identifiers'].items():
            if id_type != 'doi':
                if id_type.lower() in ['pmid', 'pmcid']:
                    identifiers_and_links.append(f"{id_type.upper()}: {id_value}")
                elif id_type.lower() in ['isbn', 'issn']:
                    identifiers_and_links.append(f"{id_type.upper()}: {id_value}")

        # Format URLs with display-friendly text
        for url in ref_data['urls']:
            if url.startswith(('http://', 'https://')):
                # Extract domain for cleaner display
                try:
                    domain = url.split('//')[1].split('/')[0]
                    identifiers_and_links.append(f"[{domain}]({url})")
                except IndexError:
                    identifiers_and_links.append(f"[{url}]({url})")
            else:
                identifiers_and_links.append(f"[{url}]({url})")

        return identifiers_and_links

    def _extract_raw_reference(self, bibl_struct: Tag) -> str | None:
        """Extract raw reference text as fallback."""
        # Look for raw reference notes
        raw_ref = bibl_struct.find("note", attrs={"type": "raw_reference"})
        if raw_ref:
            raw_text = raw_ref.get_text().strip()
            if raw_text:
                return raw_text

        # Fallback to cleaning all text content
        raw_text = bibl_struct.get_text().strip()

        # Remove reference number if present
        raw_text = re.sub(r'^\[\d+\]\s*', '', raw_text)

        # Clean up excessive whitespace
        raw_text = re.sub(r'\s+', ' ', raw_text)

        return raw_text if len(raw_text) > 20 else None


# Backwards compatible top-level function
def convert_tei_file_to_markdown(tei_file: Union[str, Path, BinaryIO]) -> Optional[str]:
    """Convert a TEI file to Markdown format.
    
    Args:
        tei_file: Path to TEI file or file-like object
        
    Returns:
        Markdown content as string, or None if conversion fails
    """
    converter = TEI2MarkdownConverter()
    return converter.convert_tei_file(tei_file)
