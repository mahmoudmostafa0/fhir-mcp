#!/usr/bin/env python3
"""
FHIR MCP server – works with mcp 1.10.1 (no on_startup/on_shutdown hooks)
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
import base64
import io

import httpx
from mcp.server.fastmcp import FastMCP

# ──────────────────────────────
# Config / logging
# ──────────────────────────────
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("fhir-mcp")

# Try to import PyPDF2 for PDF text extraction (optional)
try:
    import PyPDF2
    PDF_EXTRACTION_AVAILABLE = True
except ImportError:
    PDF_EXTRACTION_AVAILABLE = False
    log.warning("PyPDF2 not available. PDF text extraction will be disabled.")

FHIR_BASE_URL = os.getenv("FHIR_BASE_URL", "https://hapi-development.up.railway.app/fhir").rstrip("/")
FHIR_AUTH_TOKEN = os.getenv("FHIR_AUTH_TOKEN")

# ──────────────────────────────
# Simple async FHIR helper
# ──────────────────────────────
class FHIRClient:
    """Async helper for basic FHIR interactions."""

    def __init__(self, base: str, token: Optional[str] = None) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.client = httpx.AsyncClient(timeout=30)

    def _hdrs(self) -> Dict[str, str]:
        h = {
            "Accept": "application/fhir+json",
            "Content-Type": "application/fhir+json",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _req(self, method: str, endpoint: str, **kw: Any) -> Dict[str, Any]:
        url = f"{self.base}/{endpoint.lstrip('/')}"
        try:
            r = await self.client.request(method, url, headers=self._hdrs(), **kw)
            if r.status_code in (401, 403, 404):
                return {
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": f"http-{r.status_code}",
                            "details": {"text": r.text},
                        }
                    ],
                }
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            return {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "exception", "details": {"text": str(e)}}],
            }

    # typed helpers
    async def get_patient(self, pid: str) -> Dict[str, Any]:
        return await self._req("GET", f"Patient/{pid}")

    async def search(self, rt: str, **params: Any) -> Dict[str, Any]:
        return await self._req("GET", rt, params=params)
        
    async def create(self, resource_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new FHIR resource."""
        return await self._req("POST", resource_type, json=data)


# ──────────────────────────────
# MCP server
# ──────────────────────────────
mcp = FastMCP(
    "FHIR Server",
    description="A comprehensive FHIR-compliant server that provides a robust set of tools for securely managing and accessing healthcare data. It supports a wide range of FHIR resources, enabling seamless interaction with patient information, clinical records, and administrative data.",
    json_response=True,
    host="0.0.0.0"
)

# Lazy singleton for the shared HTTP client
_client: Optional[FHIRClient] = None


def _get_client() -> FHIRClient:
    global _client
    if _client is None:
        log.info("Initialising FHIR client for %s", FHIR_BASE_URL)
        _client = FHIRClient(FHIR_BASE_URL, FHIR_AUTH_TOKEN)
    return _client


# ──────────────────────────────
# Helper formatting
# ──────────────────────────────
def _human_name(pt: Dict[str, Any]) -> str:
    if pt.get("name"):
        n = pt["name"][0]
        return f"{' '.join(n.get('given', []))} {n.get('family', '')}".strip()
    return "Unknown"


def _format_address(address: Dict[str, Any]) -> str:
    """Format an address dictionary into a single string."""
    lines = address.get("line", [])
    city = address.get("city", "")
    state = address.get("state", "")
    postal_code = address.get("postalCode", "")
    country = address.get("country", "")
    
    address_parts = lines + [f"{city}, {state} {postal_code}", country]
    return ", ".join(filter(None, address_parts))

def _pt_summary(pt: Dict[str, Any]) -> str:
    return f"🆔 {pt.get('id','?')} | {_human_name(pt)} | DOB {pt.get('birthDate','?')} | {pt.get('gender','?')}"

def _practitioner_summary(practitioner: Dict[str, Any]) -> str:
    summary = f"🆔 {practitioner.get('id','?')} | {_human_name(practitioner)}"
    
    # Extract specialty from qualification if available
    specialty = None
    if practitioner.get('qualification'):
        for qual in practitioner.get('qualification', []):
            if qual.get('code', {}).get('coding'):
                coding = qual['code']['coding'][0]
                specialty = coding.get('display')
                if specialty:
                    break
                
    # Add specialty to summary if available
    if specialty:
        summary += f" | 👨‍⚕️ {specialty}"
    
    # Add address to summary if available
    if practitioner.get('address'):
        summary += f" | 🏥 {_format_address(practitioner['address'][0])}"
    
    return summary


def _format_organization(org: Dict[str, Any]) -> Dict[str, Any]:
    """Format an organization resource to a more concise representation."""
    # Extract organization type if available
    org_type = None
    if org.get('type') and len(org.get('type', [])) > 0:
        type_coding = org.get('type', [{}])[0].get('coding', [])
        if type_coding and len(type_coding) > 0:
            org_type = type_coding[0].get('display')
    
    # Extract contact information
    contact_info = {}
    for telecom in org.get('telecom', []):
        system = telecom.get('system')
        if system:
            contact_info[system] = telecom.get('value')
    
    # Extract address information
    address_info = None
    if org.get('address') and len(org.get('address', [])) > 0:
        addr = org.get('address', [{}])[0]
        address_info = {
            'line': addr.get('line', []),
            'city': addr.get('city'),
            'state': addr.get('state'),
            'postalCode': addr.get('postalCode'),
            'country': addr.get('country')
        }
    
    # Create formatted organization
    formatted_org = {
        'id': org.get('id'),
        'name': org.get('name', 'Unnamed Organization'),
        'active': org.get('active', True),
        'type': org_type
    }
    
    # Add contact info if available
    if contact_info:
        formatted_org['contact'] = contact_info
    
    # Add address if available
    if address_info:
        formatted_org['address'] = address_info
    
    # Add identifiers if available
    if org.get('identifier'):
        formatted_org['identifiers'] = [{
            'system': identifier.get('system'),
            'value': identifier.get('value')
        } for identifier in org.get('identifier', [])]
    
    return formatted_org


def _entries(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    return bundle.get("entry", []) if bundle.get("resourceType") == "Bundle" else []




# ──────────────────────────────
# Tools (docstring 1st line = description)
# ──────────────────────────────
@mcp.tool()
async def get_document_content(document_reference_id: str, extract_text: bool = False) -> Dict[str, Any]:
    """Get the content of a PDF document from a DocumentReference resource.

    This tool retrieves a specified DocumentReference, extracts the PDF content from it,
    and optionally converts the PDF content to plain text.

    Args:
        document_reference_id: The ID of the DocumentReference resource.
        extract_text: If True, extracts and returns the text content from the PDF.
                      If False (default), returns the base64-encoded PDF content.

    Returns:
        A dictionary containing the document content. If 'extract_text' is True,
        the dictionary will have a 'text_content' key. Otherwise, it will have
        a 'pdf_content_base64' key.
    """
    cli = _get_client()
    
    # First, get the DocumentReference
    doc_ref = await cli._req("GET", f"DocumentReference/{document_reference_id}")
    
    if doc_ref.get("resourceType") == "OperationOutcome":
        return {
            "error": "DocumentReference not found",
            "details": doc_ref["issue"][0]["details"]["text"]
        }
    
    # Extract PDF URL from content attachment
    content_list = doc_ref.get("content", [])
    if not content_list:
        return {"error": "No content found in DocumentReference"}
    
    attachment = content_list[0].get("attachment", {})
    pdf_url = attachment.get("url")
    content_type = attachment.get("contentType", "")
    title = attachment.get("title", "Unknown")
    
    if not pdf_url:
        return {"error": "No URL found in document attachment"}
    
    # Download the PDF content
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(pdf_url)
            response.raise_for_status()
            
            pdf_content = response.content
            pdf_size = len(pdf_content)
            
            result = {
                "document_reference_id": document_reference_id,
                "title": title,
                "content_type": content_type,
                "url": pdf_url,
                "size_bytes": pdf_size,
                # "content_base64": base64.b64encode(pdf_content).decode("utf-8")
            }
            
            # Extract text if requested and PyPDF2 is available
            if extract_text and PDF_EXTRACTION_AVAILABLE and content_type.lower() == "application/pdf":
                try:
                    pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_content))
                    text_content = ""
                    for page in pdf_reader.pages:
                        text_content += page.extract_text() + "\n"
                    
                    result["extracted_text"] = text_content.strip()
                    result["page_count"] = len(pdf_reader.pages)
                    
                except Exception as e:
                    result["text_extraction_error"] = str(e)
            
            elif extract_text and not PDF_EXTRACTION_AVAILABLE:
                result["text_extraction_error"] = "PyPDF2 not available. Install with: pip install PyPDF2"
            
            return result
            
    except httpx.HTTPError as e:
        return {
            "error": "Failed to download PDF",
            "details": str(e),
            "url": pdf_url
        }
    except Exception as e:
        return {
            "error": "Unexpected error",
            "details": str(e)
        }


@mcp.tool()
async def get_patient(patient_id: str) -> Dict[str, Any]:
    """Get a specific patient by their ID.

    Retrieves the full FHIR Patient resource for a given patient ID.

    Args:
        patient_id: The logical ID of the patient to retrieve.

    Returns:
        A dictionary representing the FHIR Patient resource.
    """
    r = await _get_client().get_patient(patient_id)
    # if r.get("resourceType") == "OperationOutcome":
    #     return r
    return r


@mcp.tool()
async def search_patients(
    first_name: str | None = None,
    family_name: str | None = None,
    count: int = 10,
) -> List[str]:
    """
    Search for patients and return a compact, human-readable summary of each match.

    Instead of returning the full FHIR Patient resources (which are extremely verbose),
    this helper extracts only the most relevant high-level fields so that the response
    remains small and LLM-friendly.

    Each returned dictionary may contain:
        • id – logical patient id
        • name – concatenated human name
        • gender
        • birthDate

    Args:
        first_name: Optional given name filter (FHIR `name` search param).
        family_name: Optional family name filter (FHIR `family` search param).
        count: Max number of patients to return (default 10).

    Returns:
        List of compact dictionaries describing the matching patients.
    """
    params: Dict[str, Any] = {"_count": count}
    if first_name:
        params["name"] = first_name
    if family_name:
        params["family"] = family_name

    b = await _get_client().search("Patient", **params)
    return [_pt_summary(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_all_patients(count: int = 10) -> List[str]:
    """Get all patients (no filters).

    Retrieves a list of all patient resources from the FHIR server, without applying any filters.

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Patient resource.
    """
    return await search_patients(count=count)


@mcp.tool()
async def search_practitioners(name: str | None = None, family: str | None = None, count: int = 10) -> List[str]:
    """
    Find *doctors* (FHIR **Practitioner** resources) on the connected FHIR server.

    In FHIR, a "Practitioner" represents a healthcare professional, This helper queries
    the Practitioner endpoint using the standard search parameters 

    Args:
        name: The practitioner's given name to search for.
        family: The practitioner's family name to search for.
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Practitioner resource.
    """
    params = {"_count": count}
    if name:
        params["name"] = name
    if family:
        params["family"] = family
    b = await _get_client().search("Practitioner", **params)
    return [_practitioner_summary(e["resource"]) for e in _entries(b)]
    


@mcp.tool()
async def search_observations(
    patient: str | None = None, 
    code: str | None = None,
    category: str | None = None,
    date: str | None = None,
    status: str | None = None,
    count: int = 10,
    follow_pagination: bool = False,
    max_pages: int = 3
) -> Dict[str, Any]:
    """Search for observations with enhanced filtering and pagination support.

    Retrieves observation resources with various filtering options and pagination handling.
    Results are always returned in a summarized format for better readability and performance.
    Uses for the Observation resource include:
    Vital signs such as body weight, blood pressure, and temperature
    Laboratory Data like blood glucose, or an estimated GFR
    Imaging results like bone density or fetal measurements
    Clinical Findings* such as abdominal tenderness
    Device measurements such as EKG data or Pulse Oximetry data
    Device Settings such as mechanical ventilator parameters.
    Clinical assessment tools such as APGAR or a Glasgow Coma Score
    Personal characteristics: such as eye-color
    Social history like tobacco use, family support, or cognitive status
    Core characteristics like pregnancy status, or a death assertion
    Product quality tests such as pH, Assay, Microbial limits, etc. on product, substance, or an environment.

    Args:
        patient: The ID of the patient to retrieve observations for.
        code: LOINC or SNOMED code to filter observations by type (e.g., '4548-4' for HbA1c).
        category: Category of observation (e.g., 'laboratory', 'vital-signs').
        date: Date filter in FHIR format (e.g., 'gt2023-01-01', 'lt2023-12-31', '2023-06-01').
        status: Status of the observation (e.g., 'final', 'preliminary').
        count: The maximum number of results to return per page (default is 10).
        follow_pagination: If True, follows pagination links to retrieve all matching observations.
        max_pages: Maximum number of pages to retrieve when follow_pagination is True.

    Returns:
        A dictionary with total count and summarized observation data.
    """
    # Build query parameters
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if code:
        params["code"] = code
    if category:
        params["category"] = category
    if date:
        params["date"] = date
    if status:
        params["status"] = status
    
    # Initial search
    result = await _get_client().search("Observation", **params)
    
    # Handle pagination if requested
    if follow_pagination and "link" in result:
        next_link = next((link["url"] for link in result.get("link", []) if link.get("relation") == "next"), None)
        pages_retrieved = 1
        
        while next_link and pages_retrieved < max_pages:
            # Extract the URL path and query parameters
            if "_getpages" in next_link:
                # Handle HAPI FHIR pagination format
                next_page = await _get_client()._req("GET", next_link.split("/fhir/")[1] if "/fhir/" in next_link else next_link.split("/fhir")[1])
            else:
                # Handle standard FHIR pagination
                path_parts = next_link.split(FHIR_BASE_URL)[1] if FHIR_BASE_URL in next_link else next_link
                next_page = await _get_client()._req("GET", path_parts)
                
            # Add entries from next page to our result
            if "entry" in next_page and next_page["entry"]:
                result["entry"].extend(next_page["entry"])
                
            # Update next link
            next_link = next((link["url"] for link in next_page.get("link", []) if link.get("relation") == "next"), None)
            pages_retrieved += 1
    
    # Always summarize the observations
    summary = {
        "total": result.get("total", 0),
        "count": len(result.get("entry", [])),
        "observations": []
    }
    
    for entry in result.get("entry", []):
        if "resource" not in entry:
            continue
            
        obs = entry["resource"]
        obs_summary = {
            "id": obs.get("id"),
            "status": obs.get("status"),
            "date": obs.get("effectiveDateTime"),
            "type": _extract_coding_display(obs.get("code", {}).get("coding", [])),
            "category": _extract_categories(obs.get("category", [])),
        }
        
        # Handle different value types
        if "valueQuantity" in obs:
            obs_summary["value"] = {
                "value": obs["valueQuantity"].get("value"),
                "unit": obs["valueQuantity"].get("unit")
            }
        elif "valueString" in obs:
            obs_summary["value"] = obs["valueString"]
        elif "valueBoolean" in obs:
            obs_summary["value"] = obs["valueBoolean"]
        elif "valueInteger" in obs:
            obs_summary["value"] = obs["valueInteger"]
        elif "valueCodeableConcept" in obs:
            obs_summary["value"] = _extract_coding_display(obs["valueCodeableConcept"].get("coding", []))
        elif "component" in obs:
            # Handle component-based observations like blood pressure
            components = []
            for component in obs["component"]:
                comp_data = {
                    "type": _extract_coding_display(component.get("code", {}).get("coding", [])),
                }
                
                if "valueQuantity" in component:
                    comp_data["value"] = {
                        "value": component["valueQuantity"].get("value"),
                        "unit": component["valueQuantity"].get("unit")
                    }
                components.append(comp_data)
            obs_summary["components"] = components
            
        # Add notes if present
        if "note" in obs and obs["note"]:
            obs_summary["notes"] = [note.get("text") for note in obs["note"] if "text" in note]
            
        summary["observations"].append(obs_summary)
        
    return summary


def _extract_coding_display(codings: List[Dict[str, Any]]) -> str:
    """Extract display text from a list of codings."""
    if not codings:
        return ""
    for coding in codings:
        if "display" in coding:
            return coding["display"]
    # Fallback to code if no display
    for coding in codings:
        if "code" in coding:
            return coding["code"]
    return ""


def _extract_categories(categories: List[Dict[str, Any]]) -> List[str]:
    """Extract category names from category objects."""
    result = []
    for category in categories:
        if "coding" in category:
            for coding in category["coding"]:
                if "code" in coding:
                    result.append(coding["code"])
    return result


@mcp.tool()
async def get_capability_statement() -> Dict[str, Any]:
    """Get FHIR server capabilities.

    Retrieves the server's capability statement, which provides a summary of the
    FHIR resources, interactions, and operations supported by the server.

    Returns:
        A dictionary representing the FHIR CapabilityStatement resource.
    """
    return await _get_client().search("metadata")


@mcp.tool()
async def search_conditions(
    patient: str | None = None,
    code: str | None = None,
    clinical_status: str | None = None,
    count: int = 10,
) -> Dict[str, Any]:
    """Search for conditions/diagnoses (e.g., diabetes).

    This tool searches for clinical conditions or diagnoses, with options to filter
    by patient, condition code, and clinical status.

    Args:
        patient: The ID of the patient to search for conditions.
        code: A code representing the condition (e.g., from SNOMED CT).
        clinical_status: The clinical status of the condition (e.g., 'active', 'inactive').
        count: The maximum number of results to return (default is 10).

    Returns:
        A dictionary with simplified condition resources containing only essential fields.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if code:
        params["code"] = code
    if clinical_status:
        params["clinical-status"] = clinical_status
    
    # Get the full FHIR bundle
    bundle = await _get_client().search("Condition", **params)
    
    # Extract only the essential information
    simplified_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": bundle.get("total", 0),
        "entry": []
    }
    
    # Process each entry to extract only needed fields
    for entry in _entries(bundle):
        resource = entry.get("resource", {})
        
        # Extract essential fields
        simplified_resource = {
            "id": resource.get("id"),
            "subject": resource.get("subject", {}),
        }
        
        # Extract clinical status if available
        if "clinicalStatus" in resource and "coding" in resource["clinicalStatus"]:
            codings = resource["clinicalStatus"]["coding"]
            if codings and len(codings) > 0:
                simplified_resource["clinicalStatus"] = codings[0].get("code")
        
        # Extract code information if available
        if "code" in resource and "coding" in resource["code"]:
            codings = resource["code"]["coding"]
            if codings and len(codings) > 0:
                simplified_resource["code"] = {
                    "system": codings[0].get("system"),
                    "code": codings[0].get("code"),
                    "display": codings[0].get("display")
                }
        
        # Add onset and recorded dates if available
        if "onsetDateTime" in resource:
            simplified_resource["onsetDateTime"] = resource["onsetDateTime"]
        if "recordedDate" in resource:
            simplified_resource["recordedDate"] = resource["recordedDate"]
        if "abatementDateTime" in resource:
            simplified_resource["abatementDateTime"] = resource["abatementDateTime"]
        
        # Add to the simplified bundle
        simplified_bundle["entry"].append({
            "resource": simplified_resource
        })
    
    return simplified_bundle


@mcp.tool()
async def search_medication_requests(
    patient: str | None = None,
    status: str | None = None,
    intent: str | None = None,
    count: int = 10,
) -> Dict[str, Any]:
    """Search for medication requests/prescriptions (e.g., diabetes medications).

    Searches for medication requests, which can be filtered by patient,
    status, and intent.

    Args:
        patient: The ID of the patient to search for medication requests.
        status: The status of the medication request (e.g., 'active', 'completed').
        intent: The intent of the request (e.g., 'order', 'plan').
        count: The maximum number of results to return (default is 10).

    Returns:
        A dictionary with simplified medication request resources containing only essential fields.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if status:
        params["status"] = status
    if intent:
        params["intent"] = intent
    
    # Get the full FHIR bundle
    bundle = await _get_client().search("MedicationRequest", **params)
    
    # Extract only the essential information
    simplified_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": bundle.get("total", 0),
        "entry": []
    }
    
    # Process each entry to extract only needed fields
    for entry in _entries(bundle):
        resource = entry.get("resource", {})
        
        # Extract essential fields
        simplified_resource = {
            "id": resource.get("id"),
            "status": resource.get("status"),
        }
        
        # Add intent if available
        if "intent" in resource:
            simplified_resource["intent"] = resource["intent"]
            
        # Add subject/patient reference if available
        if "subject" in resource:
            simplified_resource["subject"] = resource["subject"]
        
        # Extract medication information if available
        if "medicationCodeableConcept" in resource and "coding" in resource["medicationCodeableConcept"]:
            codings = resource["medicationCodeableConcept"]["coding"]
            if codings and len(codings) > 0:
                simplified_resource["medication"] = {
                    "system": codings[0].get("system"),
                    "code": codings[0].get("code"),
                    "display": codings[0].get("display")
                }
        
        # Add simplified dosage instructions if available
        if "dosageInstruction" in resource and resource["dosageInstruction"]:
            dosage = resource["dosageInstruction"][0]  # Take only the first dosage instruction
            simplified_dosage = {}
            
            # Extract key dosage information
            if "text" in dosage:
                simplified_dosage["text"] = dosage["text"]
            if "asNeededBoolean" in dosage:
                simplified_dosage["asNeeded"] = dosage["asNeededBoolean"]
            if "timing" in dosage and "repeat" in dosage["timing"]:
                repeat = dosage["timing"]["repeat"]
                if "frequency" in repeat and "period" in repeat and "periodUnit" in repeat:
                    simplified_dosage["frequency"] = f"{repeat['frequency']} times per {repeat['period']} {repeat['periodUnit']}"
            
            if simplified_dosage:
                simplified_resource["dosage"] = simplified_dosage
        
        # Add to the simplified bundle
        simplified_bundle["entry"].append({
            "resource": simplified_resource
        })
    
    return simplified_bundle


@mcp.tool()
async def search_diagnostic_reports(
    patient: str | None = None,
    status: str | None = None,
    category: str | None = None,
    count: int = 10,
) -> Dict[str, Any]:
    """Search for diagnostic reports (e.g., lab results, HbA1c tests).

    This tool searches for diagnostic reports, which can be filtered by patient,
    status, and category.

    Args:
        patient: The ID of the patient to search for diagnostic reports.
        status: The status of the report (e.g., 'final', 'preliminary').
        category: The category of the report (e.g., 'LAB', 'IMG').
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR DiagnosticReport resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if status:
        params["status"] = status
    if category:
        params["category"] = category
    return await _get_client().search("DiagnosticReport", **params)


@mcp.tool()
async def search_care_plans(
    patient: str | None = None,
    status: str | None = None,
    category: str | None = None,
    count: int = 10,
) -> Dict[str, Any]:
    """Search for care plans (e.g., diabetes management plans).

    Searches for patient care plans, which can be filtered by patient,
    status, and category.

    Args:
        patient: The ID of the patient to search for care plans.
        status: The status of the care plan (e.g., 'active', 'completed').
        category: The category of the care plan (e.g., 'assess-plan', 'patient-request').
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR CarePlan resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if status:
        params["status"] = status
    if category:
        params["category"] = category
    return await _get_client().search("CarePlan", **params)


@mcp.tool()
async def search_document_references(
    patient: str | None = None,
    status: str | None = None,
    type: str | None = None,
    count: int = 10,
) -> Dict[str, Any]:
    """Search for document references (e.g., clinical documents, reports).

    Searches for references to clinical documents, which can be filtered by patient,
    status, and document type.

    Args:
        patient: The ID of the patient to search for document references.
        status: The status of the document reference (e.g., 'current', 'superseded').
        type: The type of the document (e.g., '11506-3' for 'Consultation note').
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR DocumentReference resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if status:
        params["status"] = status
    if type:
        params["type"] = type
    return await _get_client().search("DocumentReference", **params)


@mcp.tool()
async def find_patients_with_conditions(code: str | None = None, count: int = 100) -> List[str]:
    """Find unique patient IDs from condition records.

    This tool is useful for discovering patients who have a specific condition, even if their
    primary patient records are not directly accessible. It queries condition records
    and extracts the unique patient IDs associated with them.

    Args:
        code: The condition code to search for (e.g., from SNOMED CT).
        count: The maximum number of patient IDs to return (default is 100).

    Returns:
        A list of unique patient ID strings.
    """
    bundle = await search_conditions(code=code, count=count)
    pids: Set[str] = {
        e["resource"]["subject"]["reference"].split("/")[-1]
        for e in _entries(bundle)
        if "subject" in e["resource"]
    }
    return sorted(pids)


# @mcp.tool()
# async def assess_data_quality(resource_type: str | None = None) -> Dict[str, Any]:
#     """Assess the data quality and integrity of the FHIR server"""
#     resources = [resource_type] if resource_type else [
#         "Patient", "Observation", "Condition", "MedicationRequest", "Organization", "Coverage"
#     ]
#     cli = _get_client()
#     report: Dict[str, Any] = {
#         "server": FHIR_BASE_URL,
#         "generated": datetime.utcnow().isoformat(),
#         "resources": {},
#     }
#     for rt in resources:
#         b = await cli.search(rt, _count=10)
#         entries = _entries(b)
#         total = b.get("total", len(entries))
#         orphan = 0
#         if rt == "Condition":
#             orphan = len(
#                 {e["resource"]["subject"]["reference"].split("/")[-1] for e in entries if "subject" in e["resource"]}
#             )
#         report["resources"][rt] = {"total": total, "returned": len(entries), "orphan_refs_guess": orphan}
#     return report


@mcp.tool()
async def search_medicines_online(
    medicine_name: str,
    from_index: int = 1,
    size: int = 30,
    is_trending: bool = False,
    pharmacy_type_id: int = 0
) -> Dict[str, Any]:
    """Search for medicines and get their information online (price,active_ingredients) from Vezeeta pharmacy database based on medicine name"""
    
    # Vezeeta API endpoint
    url = "https://v-gateway.vezeetaservices.com/inventory/api/V2/ProductShapes"
    
    # Request parameters
    params = {
        "query": medicine_name,
        "from": from_index,
        "size": size,
        "isTrending": str(is_trending).lower(),
        "pharmacyTypeId": pharmacy_type_id,
        "version": 2
    }
    
    # Headers as specified in the curl request
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-us",
        "cache-control": "no-cache",
        "origin": "https://www.vezeeta.com",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": "https://www.vezeeta.com/",
        "sec-ch-ua": '"Brave";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
        "sec-gpc": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    }
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            
            # Format the response for better readability
            result = {
                "search_query": medicine_name,
                "total_count": data.get("totalCount", 0),
                "from": data.get("from", from_index),
                "size": data.get("size", size),
                "medicines": []
            }
            
            # Process each product
            for product in data.get("productShapes", []):
                medicine_info = {
                    "id": product.get("id"),
                    "name_en": product.get("productNameEn"),
                    "name_ar": product.get("productNameAr"),
                    "price": product.get("newPrice"),
                    "currency": product.get("currencyEn"),
                    "category": product.get("category"),
                    "shape_type": product.get("productShapeTypeName"),
                    "shape_type_ar": product.get("productShapeTypeNameAr"),
                    "stock_quantity": product.get("stockQuantity"),
                    "max_available_quantity": product.get("maxAvailableQuantity"),
                    "stock_level_id": product.get("stockLevelId"),
                    "image_url": product.get("mainImageUrl"),
                    "active_ingredients": []
                }
                
                # Extract active ingredients
                for ingredient in product.get("activeIngrediant", []):
                    if ingredient.get("lang") == "en":
                        medicine_info["active_ingredients"].append({
                            "name_en": ingredient.get("name"),
                            "country": ingredient.get("country")
                        })
                
                # Add availability info
                availability = product.get("productAvaialabilities", {})
                medicine_info["available_in_pharmacies"] = availability.get("avialableInPharmaciesCount", 0)
                
                result["medicines"].append(medicine_info)
            
            return result
            
    except httpx.HTTPError as e:
        return {
            "error": "Failed to search medicines",
            "details": str(e),
            "query": medicine_name
        }
    except Exception as e:
        return {
            "error": "Unexpected error while searching medicines",
            "details": str(e),
            "query": medicine_name
        }


@mcp.tool()
async def search_organizations(name: str | None = None, identifier: str | None = None, count: int = 10) -> Dict[str, Any]:
    """Search for organizations in the FHIR server.

    Searches for healthcare organizations, which can be filtered by name or identifier.

    Args:
        name: The name of the organization to search for.
        identifier: A unique identifier for the organization.
        count: The maximum number of results to return (default is 10).

    Returns:
        A dictionary containing total count and a list of formatted organization resources.
    """
    params = {"_count": count}
    if name:
        params["name"] = name
    if identifier:
        params["identifier"] = identifier
    b = await _get_client().search("Organization", **params)
    entries = _entries(b)
    
    # Format the response
    return {
        "total": b.get("total", len(entries)),
        "count": len(entries),
        "organizations": [_format_organization(e["resource"]) for e in entries]
    }


@mcp.tool()
async def search_all_organizations(count: int = 10) -> Dict[str, Any]:
    """Get all organizations (no filters).

    Retrieves a list of all organization resources from the FHIR server, without applying any filters.

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A dictionary containing total count and a list of formatted organization resources.
    """
    return await search_organizations(count=count)


@mcp.tool()
async def search_coverages(patient: str | None = None, status: str | None = None, count: int = 10) -> List[Dict[str, Any]]:
    """Search for coverage/insurance resources in the FHIR server.

    Searches for patient coverage information, which can be filtered by patient or status.
    Coverage resources contain detailed information about a patient's insurance coverage, including:
    - Coverage status (active, cancelled, etc.)
    - Coverage type (e.g., Extended health)
    - Subscriber and beneficiary information (patient references)
    - Payor organization references (insurance companies)
    - Plan class details (plan name, value, type)
    
    Args:
        patient: The ID of the patient (beneficiary) to search for coverage.
        status: The status of the coverage (e.g., 'active', 'cancelled').
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Coverage resource.
    """
    params = {"_count": count}
    if patient:
        params["beneficiary"] = patient
    if status:
        params["status"] = status
    b = await _get_client().search("Coverage", **params)
    return [(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_all_coverages(count: int = 10) -> List[Dict[str, Any]]:
    """Get all coverage/insurance resources (no filters).

    Retrieves a list of all coverage resources from the FHIR server, without applying any filters.
    Coverage resources contain detailed information about a patient's insurance coverage, including:
    - Coverage status (active, cancelled, etc.)
    - Coverage type (e.g., Extended health)
    - Subscriber and beneficiary information (patient references)
    - Payor organization references (insurance companies)
    - Plan class details (plan name, value, type)

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Coverage resource.
    """
    return await search_coverages(count=count)  # type: ignore[arg-type]


@mcp.tool()
async def search_related_persons(
    patient: str | None = None, relationship: str | None = None, count: int = 10
) -> List[Dict[str, Any]]:
    """Search for related persons in the FHIR server.

    Searches for persons related to a patient, which can be filtered by patient or relationship type.
    RelatedPerson resources contain detailed information about individuals with a personal or professional
    relationship to a patient, including:
    - Patient reference (the patient they are related to)
    - Relationship type (spouse, child, parent, etc.)
    - Personal information (name, gender, birth date)
    - Contact information (when available)
    
    Args:
        patient: The ID of the patient to search for related persons.
        relationship: The relationship type code (e.g., 'SPS' for spouse, 'CHILD' for child, 'FTH' for father).
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR RelatedPerson resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if relationship:
        params["relationship"] = relationship
    b = await _get_client().search("RelatedPerson", **params)
    return [(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_all_related_persons(count: int = 10) -> List[Dict[str, Any]]:
    """Get all related persons (no filters).

    Retrieves a list of all related person resources from the FHIR server, without applying any filters.
    RelatedPerson resources contain detailed information about individuals with a personal or professional
    relationship to a patient, including:
    - Patient reference (the patient they are related to)
    - Relationship type (spouse, child, parent, etc.)
    - Personal information (name, gender, birth date)
    - Contact information (when available)

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR RelatedPerson resource.
    """
    return await search_related_persons(count=count)


@mcp.tool()
async def get_insurance_plan(insurance_plan_id: str) -> Dict[str, Any]:
    """Get a specific insurance plan by its ID.

    Retrieves the full FHIR InsurancePlan resource, which contains comprehensive details about an insurance product,
    including:
    - Plan name and status (active/inactive)
    - Owning and administering organizations
    - Network of providers (hospitals, pharmacies, etc.)
    - Plan type (medical, dental, etc.)
    - Specific costs and benefits by category (hospital services, pharmacy, etc.)
    - Copayment percentages and other cost-sharing details

    Args:
        insurance_plan_id: The logical ID of the insurance plan to retrieve.

    Returns:
        A dictionary representing the FHIR InsurancePlan resource.
    """
    return await _get_client()._req("GET", f"InsurancePlan/{insurance_plan_id}")


@mcp.tool()
async def search_insurance_plans(
    owned_by: str | None = None, administered_by: str | None = None, name: str | None = None, count: int = 10
) -> List[Dict[str, Any]]:
    """Search for insurance plans (e.g., specific health insurance products).

    Searches for insurance plans, which contain comprehensive details about insurance products, including:
    - Plan name and status (active/inactive)
    - Owning and administering organizations
    - Network of providers (hospitals, pharmacies, etc.)
    - Plan type (medical, dental, etc.)
    - Specific costs and benefits by category (hospital services, pharmacy, etc.)
    - Copayment percentages and other cost-sharing details
    
    Can be filtered by the owning organization, administering organization, and plan name.

    Args:
        owned_by: The organization that owns the insurance plan.
        administered_by: The organization that administers the insurance plan.
        name: The name of the insurance plan.
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR InsurancePlan resource.
    """
    params = {"_count": count}
    if owned_by:
        params["owned-by"] = owned_by
    if administered_by:
        params["administered-by"] = administered_by
    if name:
        params["name"] = name
    b = await _get_client().search("InsurancePlan", **params)
    return [(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_all_insurance_plans(count: int = 10) -> List[Dict[str, Any]]:
    """Get all insurance plans (no filters).

    Retrieves a list of all insurance plan resources from the FHIR server. Each resource contains
    details about an insurance product, including the plan's coverage, provider network, and costs.

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR InsurancePlan resource.
    """
    return await search_insurance_plans(count=count)  # type: ignore[arg-type]


@mcp.tool()
async def search_encounters(
    patient: str | None = None, status: str | None = None, count: int = 10
) -> List[Dict[str, Any]]:
    """Search for encounters (e.g., hospital visits, appointments).

    Searches for patient encounters, which can be filtered by patient or encounter status.

    Args:
        patient: The ID of the patient to search for encounters.
        status: The status of the encounter (e.g., 'in-progress', 'finished').
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Encounter resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if status:
        params["status"] = status
    b = await _get_client().search("Encounter", **params)
    return [(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_all_encounters(count: int = 10) -> List[Dict[str, Any]]:
    """Get all encounters (no filters).

    Retrieves a list of all encounter resources from the FHIR server, without applying any filters.

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Encounter resource.
    """
    return await search_encounters(count=count)  # type: ignore[arg-type]


@mcp.tool()
async def search_allergy_intolerances(
    patient: str | None = None, count: int = 10
) -> List[Dict[str, Any]]:
    """Search for allergy intolerances.

    Searches for allergy and intolerance records for a specific patient. AllergyIntolerance resources contain
    detailed information about a patient's allergies and intolerances, including:
    - Type (allergy, intolerance)
    - Category (food, medication, environment, biologic)
    - Criticality level (high, low, unable-to-assess)
    - Specific allergen code and display text
    - Patient reference
    - Record date

    Args:
        patient: The ID of the patient to search for allergy intolerances.
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR AllergyIntolerance resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    b = await _get_client().search("AllergyIntolerance", **params)
    return [(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_all_allergy_intolerances(count: int = 10) -> List[Dict[str, Any]]:
    """Get all allergy intolerances (no filters).

    Retrieves a list of all allergy intolerance resources from the FHIR server, without applying any filters.
    AllergyIntolerance resources contain detailed information about patients' allergies and intolerances, including:
    - Type (allergy, intolerance)
    - Category (food, medication, environment, biologic)
    - Criticality level (high, low, unable-to-assess)
    - Specific allergen code and display text
    - Patient reference
    - Record date

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR AllergyIntolerance resource.
    """
    return await search_allergy_intolerances(count=count)  # type: ignore[arg-type]


@mcp.tool()
async def search_procedures(
    patient: str | None = None, count: int = 10
) -> List[Dict[str, Any]]:
    """Search for procedures.

    Searches for clinical procedures performed on a patient.

    Args:
        patient: The ID of the patient to search for procedures.
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Procedure resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    b = await _get_client().search("Procedure", **params)
    return [(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_all_procedures(count: int = 10) -> List[Dict[str, Any]]:
    """Get all procedures (no filters).

    Retrieves a list of all procedure resources from the FHIR server, without applying any filters.

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Procedure resource.
    """
    return await search_procedures(count=count)  # type: ignore[arg-type]


@mcp.tool()
async def search_immunization(
    patient: str | None = None,
    date: str | None = None,
    status: str | None = None,
    vaccine_code: str | None = None,
    manufacturer: str | None = None,
    lot_number: str | None = None,
    immun_id: str | None = None,
    immun_lastUpdated: str | None = None,
    count: int = 10
) -> dict:
    """
    Search for Immunization resources using FHIR-compliant parameters.

    This tool allows searching for Immunization records using a variety of FHIR search parameters, including patient reference, date, status, vaccine code, manufacturer, lot number, resource ID, and last updated timestamp.

    Args:
        patient: Search by patient reference (e.g., 'Patient/605982' or '605982').
        date: Search by the date of immunization (e.g., '2024-01-01', 'ge2023-01-01&date=le2024-01-01').
        status: Immunization status (e.g., 'completed', 'entered-in-error').
        vaccine_code: Vaccine CVX/SNOMED code (e.g., '140', 'http://hl7.org/fhir/sid/cvx|140').
        manufacturer: Search by vaccine manufacturer organization (e.g., 'Organization/123').
        lot_number: Search by lot number (e.g., 'FLU2024A').
        immun_id: Search by resource ID (maps to FHIR parameter '_id', e.g., '606048').
        immun_lastUpdated: Search by when the record was updated (maps to FHIR parameter '_lastUpdated', e.g., 'ge2024-01-01').
        count: The maximum number of results to return (default is 10).

    Returns:
        A FHIR Bundle containing matching Immunization resources.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if date:
        params["date"] = date
    if status:
        params["status"] = status
    if vaccine_code:
        params["vaccine-code"] = vaccine_code
    if manufacturer:
        params["manufacturer"] = manufacturer
    if lot_number:
        params["lot-number"] = lot_number
    if immun_id:
        params["_id"] = immun_id
    if immun_lastUpdated:
        params["_lastUpdated"] = immun_lastUpdated
    return await _get_client().search("Immunization", **params)


@mcp.tool()
async def get_immunization(immun_id: str) -> dict:
    """
    Get a specific Immunization resource by its ID.

    Retrieves the full FHIR Immunization resource for a given immunization ID. The Immunization resource records details about the administration of a vaccine to a patient.

    Args:
        immun_id: The logical ID of the Immunization resource to retrieve.

    Returns:
        A dictionary representing the FHIR Immunization resource.


    Reference:
        https://build.fhir.org/immunization.html
    """
    return await _get_client()._req("GET", f"Immunization/{immun_id}")


# (Legacy)


@mcp.tool()
async def search_all_immunizations(count: int = 10) -> List[Dict[str, Any]]:
    """Get all immunization records (no filters).

    Retrieves a list of all immunization resources from the FHIR server, without applying any filters.

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Immunization resource.
    """
    return await search_immunizations(count=count)  # type: ignore[arg-type]


@mcp.tool()
async def search_locations(
    name_query: str | None = None, address_query: str | None = None, count: int = 10
) -> List[Dict[str, Any]]:
    """Search for locations (e.g., hospitals, pharmacies, clinics).

    Searches for healthcare facility locations, which can be filtered by name or address.
    Location resources contain detailed information about healthcare facilities, including:
    - Facility name
    - Address information (street, city, country)
    - Managing organization reference
    
    Args:
        name_query: A portion of the location's name or alias to search for.
        address_query: A server defined search that may match one of the string fields in the Address, including line, city, district, state, country, postalCode, and/or text
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Location resource.
    """
    params = {"_count": count}
    if name_query:
        params["name"] = name_query
    if address_query:
        params["address"] = address_query
    b = await _get_client().search("Location", **params)
    return [(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_all_locations(count: int = 10) -> List[Dict[str, Any]]:
    """Get all locations (no filters).

    Retrieves a list of all location resources from the FHIR server, without applying any filters.
    Location resources contain detailed information about healthcare facilities, including:
    - Facility name
    - Address information (street, city, country)
    - Managing organization reference
    
    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Location resource.
    """
    return await search_locations(count=count)  # type: ignore[arg-type]


@mcp.tool()
async def search_practitioner_roles(
    practitioner: str | None = None, organization: str | None = None, specialty: str | None = None, count: int = 10
) -> List[Dict[str, Any]]:
    """Search for practitioner roles (e.g., doctors at specific facilities).

    Searches for practitioner roles, which link practitioners to organizations with specific roles.
    PractitionerRole resources contain detailed information about healthcare providers' roles, including:
    - Active status
    - Practitioner reference (the healthcare provider)
    - Organization reference (the healthcare facility)
    - Role codes (doctor, nurse, etc.)
    - Location references (where the practitioner works)
    
    Args:
        practitioner: The ID of the practitioner to search for roles.
        organization: The ID of the organization to search for practitioners.
        specialty: The specialty code to search for.
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR PractitionerRole resource.
    """
    params = {"_count": count}
    if practitioner:
        params["practitioner"] = practitioner
    if organization:
        params["organization"] = organization
    if specialty:
        params["specialty"] = specialty
    b = await _get_client().search("PractitionerRole", **params)
    return [(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_all_practitioner_roles(count: int = 10) -> List[Dict[str, Any]]:
    """Get all practitioner roles (no filters).

    Retrieves a list of all practitioner role resources from the FHIR server, without applying any filters.
    PractitionerRole resources contain detailed information about healthcare providers' roles, including:
    - Active status
    - Practitioner reference (the healthcare provider)
    - Organization reference (the healthcare facility)
    - Role codes (doctor, nurse, etc.)
    - Location references (where the practitioner works)
    
    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR PractitionerRole resource.
    """
    return await search_practitioner_roles(count=count)  # type: ignore[arg-type]






# ---------- BRCA1 or Family Cancer History ----------
@mcp.tool()
async def check_genetic_cancer_risk(patient_id: str) -> Optional[str]:
    """
    Assess the patient's risk of hereditary cancer based on BRCA1 variant or family history.

    This function is used when a doctor wants to determine whether the patient may be at increased
    risk of developing cancer (e.g., breast cancer) due to genetic factors or family history.
    It checks two key sources:
    1. MolecularSequence for known BRCA1 gene variants (indicating hereditary breast/ovarian cancer risk).
    2. FamilyMemberHistory for relatives with recorded cancer conditions.

    Args:
        patient_id: The FHIR Patient resource ID.

    Returns:
        A message alerting to potential cancer risk due to genetic predisposition or family history.
        Returns None if no such indicators are found.
    """
    family = [e["resource"] for e in _entries(await _get_client().search("FamilyMemberHistory", patient=patient_id))]
    risk_conditions = [f for f in family if "cancer" in f.get("condition", [{}])[0].get("code", {}).get("text", "").lower()]
    sequences = [e["resource"] for e in _entries(await _get_client().search("MolecularSequence", patient=patient_id))]
    brca = [s for s in sequences if "brca1" in s.get("referenceSeq", {}).get("referenceSeqId", {}).get("text", "").lower()]
    if brca or risk_conditions:
        return "🧬 BRCA1 variant or family cancer history detected – consider genetic counseling."
    return None


# ---------- Early Heart Disease in Family ----------
@mcp.tool()
async def check_family_heart_history(patient_id: str) -> Optional[str]:
    """
    Check if the patient may be at risk for heart disease based on family history.

    This tool searches the FamilyMemberHistory resource to determine whether
    any close relatives had early-onset heart disease (before age 60).
    If found, it suggests that the patient may be at elevated risk and may benefit
    from preventive screening like LDL cholesterol testing.

    Args:
        patient_id: The FHIR Patient resource ID.

    Returns:
        A message if early-onset heart disease is detected in the family history, otherwise None.
    """
    family = [e["resource"] for e in _entries(await _get_client().search("FamilyMemberHistory", patient=patient_id))]
    for f in family:
        condition = f.get("condition", [{}])[0].get("code", {}).get("text", "").lower()
        onset = f.get("condition", [{}])[0].get("onsetAge", {}).get("value", 100)
        if "heart" in condition and onset < 60:
            return "🫀 Family history shows early-onset heart disease – suggest LDL screening every 6 months."
    return None


@mcp.tool()
async def create_appointment(
    patient_id: str,
    practitioner_id: str | None = None,
    start_time: str = None,
    end_time: str = None,
    status: str = "booked",
    description: str | None = None,
    appointment_type: str | None = None,
    location_id: str | None = None
) -> Dict[str, Any]:
    """Create a new appointment in the FHIR server.
    IMPORTANT: This function checks if the schedule is free before creating an appointment!
    Creates a new appointment resource with the specified details. The appointment
    will be linked to the specified patient and optionally to a practitioner.

    Args:
        patient_id: The ID of the patient for whom the appointment is being created.
        practitioner_id: The ID of the practitioner (doctor) for the appointment.
        start_time: The start time of the appointment in ISO format (e.g., '2025-07-15T10:00:00Z').
        end_time: The end time of the appointment in ISO format (e.g., '2025-07-15T10:30:00Z').
        status: The status of the appointment (e.g., 'booked', 'proposed', 'arrived').
        description: A description or reason for the appointment.
        appointment_type: The type of appointment (e.g., 'checkup', 'emergency').
        location_id: The ID of the location where the appointment will take place.

    Returns:
        A dictionary representing the created FHIR Appointment resource.
    """
    # Validate required parameters
    if not start_time or not end_time:
        return {
            "resourceType": "OperationOutcome",
            "issue": [{
                "severity": "error",
                "code": "invalid",
                "details": {"text": "Both start_time and end_time are required for creating an appointment."}
            }]
        }

    try:
        client = _get_client()
        
        # CHECK FOR EXISTING FREE APPOINTMENTS FIRST
        # Build search parameters for checking availability
        search_params = {
            "date": start_time,  # This checks for appointments on the same date
            "status": "free"
        }
        
        # Add practitioner to search if specified
        if practitioner_id:
            search_params["actor"] = f"Practitioner/{practitioner_id}"
        
        # Add location to search if specified
        if location_id:
            search_params["actor"] = f"Location/{location_id}"
        
        # Search for existing free appointments
        existing_appointments = await client.search("Appointment", **search_params)
        
        # Check if we found any free appointments in the time slot
        if existing_appointments.get("entry"):
            for entry in existing_appointments["entry"]:
                appointment_resource = entry["resource"]
                
                # Check if the time slot overlaps with our requested time
                existing_start = appointment_resource.get("start")
                existing_end = appointment_resource.get("end")
                
                if existing_start and existing_end:
                    # Convert to datetime for comparison
                    from datetime import datetime
                    import dateutil.parser
                    
                    req_start = dateutil.parser.parse(start_time)
                    req_end = dateutil.parser.parse(end_time)
                    exist_start = dateutil.parser.parse(existing_start)
                    exist_end = dateutil.parser.parse(existing_end)
                    
                    # Check if times match exactly or overlap
                    if (req_start == exist_start and req_end == exist_end) or \
                       (req_start < exist_end and req_end > exist_start):
                        
                        # Found a matching free appointment - update it instead of creating new
                        appointment_id = appointment_resource["id"]
                        
                        # Update the existing appointment
                        updated_appointment = appointment_resource.copy()
                        updated_appointment["status"] = status
                        
                        # Update participant to include the patient
                        updated_appointment["participant"] = [
                            {
                                "actor": {
                                    "reference": f"Patient/{patient_id}"
                                },
                                "status": "accepted"
                            }
                        ]
                        
                        # Add practitioner if provided
                        if practitioner_id:
                            updated_appointment["participant"].append({
                                "actor": {
                                    "reference": f"Practitioner/{practitioner_id}"
                                },
                                "status": "accepted"
                            })
                        
                        # Add location if provided
                        if location_id:
                            updated_appointment["participant"].append({
                                "actor": {
                                    "reference": f"Location/{location_id}"
                                },
                                "status": "accepted"
                            })
                        
                        # Add description if provided
                        if description:
                            updated_appointment["description"] = description
                        
                        # Add appointment type if provided
                        if appointment_type:
                            updated_appointment["appointmentType"] = {
                                "coding": [
                                    {
                                        "system": "http://terminology.hl7.org/CodeSystem/v2-0276",
                                        "code": appointment_type
                                    }
                                ]
                            }
                        
                        # Update the existing appointment
                        result = await client.update("Appointment", appointment_id, updated_appointment)
                        return result
        
        # CHECK FOR CONFLICTING APPOINTMENTS
        # Search for any appointments (not just free ones) that might conflict
        conflict_search_params = {
            "date": start_time
        }
        
        if practitioner_id:
            conflict_search_params["actor"] = f"Practitioner/{practitioner_id}"
        
        existing_appointments = await client.search("Appointment", **conflict_search_params)
        
        # Check for time conflicts
        if existing_appointments.get("entry"):
            for entry in existing_appointments["entry"]:
                appointment_resource = entry["resource"]
                
                # Skip if this appointment is cancelled or no-show
                if appointment_resource.get("status") in ["cancelled", "noshow"]:
                    continue
                
                existing_start = appointment_resource.get("start")
                existing_end = appointment_resource.get("end")
                
                if existing_start and existing_end:
                    from datetime import datetime
                    import dateutil.parser
                    
                    req_start = dateutil.parser.parse(start_time)
                    req_end = dateutil.parser.parse(end_time)
                    exist_start = dateutil.parser.parse(existing_start)
                    exist_end = dateutil.parser.parse(existing_end)
                    
                    # Check for overlap
                    if req_start < exist_end and req_end > exist_start:
                        return {
                            "resourceType": "OperationOutcome",
                            "issue": [{
                                "severity": "error",
                                "code": "conflict",
                                "details": {
                                    "text": f"Time slot conflict: An appointment already exists from {existing_start} to {existing_end}"
                                }
                            }]
                        }
        
        # NO FREE APPOINTMENTS FOUND AND NO CONFLICTS - CREATE NEW APPOINTMENT
        appointment = {
            "resourceType": "Appointment",
            "status": status,
            "start": start_time,
            "end": end_time,
            "participant": [
                {
                    "actor": {
                        "reference": f"Patient/{patient_id}"
                    },
                    "status": "accepted"
                }
            ]
        }

        # Add optional fields if provided
        if description:
            appointment["description"] = description

        if appointment_type:
            appointment["appointmentType"] = {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/v2-0276",
                        "code": appointment_type
                    }
                ]
            }

        # Add practitioner if provided
        if practitioner_id:
            appointment["participant"].append({
                "actor": {
                    "reference": f"Practitioner/{practitioner_id}"
                },
                "status": "accepted"
            })

        # Add location if provided
        if location_id:
            appointment["participant"].append({
                "actor": {
                    "reference": f"Location/{location_id}"
                },
                "status": "accepted"
            })

        # Create the appointment in the FHIR server
        result = await client.create("Appointment", appointment)
        return result
        
    except Exception as e:
        return {
            "resourceType": "OperationOutcome",
            "issue": [{
                "severity": "error",
                "code": "exception",
                "details": {"text": f"Error creating appointment: {str(e)}"}
            }]
        }

@mcp.tool()
async def get_practitioner(practitioner_id: str) -> Dict[str, Any]:
    """Get a specific practitioner (doctor) by their ID.

    Retrieves the full FHIR Practitioner (doctor) resource for a given practitioner ID.

    Args:
        practitioner_id: The logical ID of the practitioner (doctor) to retrieve.

    Returns:
        A dictionary representing the FHIR Practitioner (doctor) resource.
    """
    r = await _get_client()._req("GET", f"Practitioner/{practitioner_id}")
    return r

@mcp.tool()
async def get_organization(organization_id: str) -> Dict[str, Any]:
    """
    Get a specific organization by its ID.

    Retrieves the full FHIR Organization resource for a given organization ID.
    Organization resources represent healthcare entities such as hospitals, clinics, pharmacies, and insurance companies.
    They include details such as name, type, contact information, and address.

    Args:
        organization_id: The logical ID of the organization to retrieve.
    """
    org = await _get_client()._req("GET", f"Organization/{organization_id}")
    if org.get("resourceType") == "Organization":
        return _format_organization(org)
    return org


@mcp.tool()
async def get_practitioner_role(practitioner_role_id: str) -> Dict[str, Any]:
    """
    Get a specific practitioner (doctor) role by its ID.

    Retrieves the full FHIR PractitionerRole resource for a given practitioner role ID.
    PractitionerRole resources link practitioners (doctors) to organizations, specifying their roles,
    specialties, and healthcare services provided. This helps identify which organization a doctor works in
    and what health services they provide.

    Args:
        practitioner_role_id: The logical ID of the PractitionerRole to retrieve.
    """
    return await _get_client()._req("GET", f"PractitionerRole/{practitioner_role_id}")


@mcp.tool()
async def get_medication_statement(statement_id: str) -> Dict[str, Any]:
    """
    Get a specific medication statement by its ID.

    Retrieves the full FHIR MedicationStatement resource for a given statement ID.
    MedicationStatement resources record what medications a patient is taking, including status,
    medication details, and effective period.

    Args:
        statement_id: The logical ID of the MedicationStatement to retrieve.
    """
    return await _get_client()._req("GET", f"MedicationStatement/{statement_id}")


@mcp.tool()
async def search_medication_statements(patient_id: str, count: int = 10) -> Dict[str, Any]:
    """
    Search for medication statements for a specific patient.

    Retrieves a bundle of FHIR MedicationStatement resources for the given patient ID.
    Useful for listing all medications a patient is currently taking or has taken.

    Args:
        patient_id: The FHIR Patient resource ID.
        count: The maximum number of results to return (default is 10).

    """
    params = {"patient": patient_id, "_count": count}
    return await _get_client().search("MedicationStatement", **params)


@mcp.tool()
async def search_healthcare_service(organization_id: str, count: int = 10) -> Dict[str, Any]:
    """
    Search for healthcare services provided by a specific organization.

    Retrieves a bundle of FHIR HealthcareService resources for the given organization ID.
    HealthcareService resources describe the specific services offered by healthcare organizations,
    such as clinics, specialties, and available times.
    """
    params = {"organization": organization_id, "_count": count}
    return await _get_client().search("HealthcareService", **params)


@mcp.tool()
async def get_healthcare_service(service_id: str) -> Dict[str, Any]:
    """
    Get a specific healthcare service by its ID.

    Retrieves the full FHIR HealthcareService resource for a given service ID.
    HealthcareService resources describe the details of a service offered by a healthcare organization,
    including type, name, available times, and the organization providing the service.

    Args:
        service_id: The logical ID of the HealthcareService to retrieve.
    """
    return await _get_client()._req("GET", f"HealthcareService/{service_id}")


# ──────────────────────────────
# Entrypoint
# ──────────────────────────────
if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",     # streamable-http
        # host="0.0.0.0",
        # port=8080,            # choose any free port
        # mount_path="/mcp/",         # optional – default is /mcp/
    )
