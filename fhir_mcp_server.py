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


def _pt_summary(pt: Dict[str, Any]) -> str:
    return f"🆔 {pt.get('id','?')} | {_human_name(pt)} | DOB {pt.get('birthDate','?')} | {pt.get('gender','?')}"


# def _practitioner_summary(practitioner: Dict[str, Any]) -> str:
#     return f"🆔 {practitioner.get('id','?')} | {_human_name(practitioner)}"


# def _organization_summary(org: Dict[str, Any]) -> str:
#     return f"🆔 {org.get('id','?')} | {org.get('name', 'Unnamed Organization')} | Type: {org.get('type', [{}])[0].get('text', 'Unknown') if org.get('type') else 'Unknown'}"


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
async def get_patient(patient_id: str) -> str:
    """Get a specific patient by their ID.

    Retrieves the full FHIR Patient resource for a given patient ID.

    Args:
        patient_id: The logical ID of the patient to retrieve.

    Returns:
        A dictionary representing the FHIR Patient resource.
    """
    r = await _get_client().get_patient(patient_id)
    if r.get("resourceType") == "OperationOutcome":
        return r["issue"][0]["details"]["text"]
    return _pt_summary(r)


@mcp.tool()
async def search_patients(name: str | None = None, family: str | None = None, count: int = 10) -> List[str]:
    """Search for patients in the FHIR server.

    This tool allows searching for patients by their name, family name, or both.

    Args:
        name: The patient's given name to search for.
        family: The patient's family name to search for.
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Patient resource.
    """
    params = {"_count": count}
    if name:
        params["name"] = name
    if family:
        params["family"] = family
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
    return await search_patients(count=count)  # type: ignore[arg-type]


@mcp.tool()
async def search_practitioners(name: str | None = None, family: str | None = None, count: int = 10) -> List[Dict[str, Any]]:
    """Search for practitioners (doctors) in the FHIR server.

    This tool allows searching for practitioners by their name, family name, or both.

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
    return [(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_observations(patient: str | None = None, count: int = 10) -> Dict[str, Any]:
    """Search for observations.

    Retrieves observation resources, optionally filtered by patient.

    Args:
        patient: The ID of the patient to retrieve observations for.
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Observation resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    return await _get_client().search("Observation", **params)


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
        A list of dictionaries, where each dictionary is a FHIR Condition resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if code:
        params["code"] = code
    if clinical_status:
        params["clinical-status"] = clinical_status
    return await _get_client().search("Condition", **params)


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
        A list of dictionaries, where each dictionary is a FHIR MedicationRequest resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    if status:
        params["status"] = status
    if intent:
        params["intent"] = intent
    return await _get_client().search("MedicationRequest", **params)


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
async def search_medicines(
    medicine_name: str,
    from_index: int = 1,
    size: int = 30,
    is_trending: bool = False,
    pharmacy_type_id: int = 0
) -> Dict[str, Any]:
    """Search for medicines and get their information (price,active_ingredients) from Vezeeta pharmacy database based on medicine name"""
    
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
async def search_organizations(name: str | None = None, identifier: str | None = None, count: int = 10) -> List[Dict[str, Any]]:
    """Search for organizations in the FHIR server.

    Searches for healthcare organizations, which can be filtered by name or identifier.

    Args:
        name: The name of the organization to search for.
        identifier: A unique identifier for the organization.
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Organization resource.
    """
    params = {"_count": count}
    if name:
        params["name"] = name
    if identifier:
        params["identifier"] = identifier
    b = await _get_client().search("Organization", **params)
    return [(e["resource"]) for e in _entries(b)]


@mcp.tool()
async def search_all_organizations(count: int = 10) -> List[Dict[str, Any]]:
    """Get all organizations (no filters).

    Retrieves a list of all organization resources from the FHIR server, without applying any filters.

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Organization resource.
    """
    return await search_organizations(count=count)  # type: ignore[arg-type]




@mcp.tool()
async def search_coverages(patient: str | None = None, status: str | None = None, count: int = 10) -> List[Dict[str, Any]]:
    """Search for coverage/insurance resources in the FHIR server.

    Searches for patient coverage information, which can be filtered by patient or status.

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

    Args:
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Coverage resource.
    """
    return await search_coverages(count=count)  # type: ignore[arg-type]

@mcp.tool()
async def get_insurance_plan(insurance_plan_id: str) -> Dict[str, Any]:
    """Get a specific insurance plan by its ID.

    Retrieves the full FHIR InsurancePlan resource, which contains details about an insurance product,
    including who is offering the plan, what the coverage is, the network of providers, and costs.

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

    Searches for insurance plans, which contain details about insurance products, including who is
    offering the plan, what the coverage is, the network of providers, and costs.
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

    Searches for allergy and intolerance records for a specific patient.

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
async def search_immunizations(
    patient: str | None = None, count: int = 10
) -> List[Dict[str, Any]]:
    """Search for immunization records.

    Searches for immunization records for a specific patient.

    Args:
        patient: The ID of the patient to search for immunization records.
        count: The maximum number of results to return (default is 10).

    Returns:
        A list of dictionaries, where each dictionary is a FHIR Immunization resource.
    """
    params = {"_count": count}
    if patient:
        params["patient"] = patient
    b = await _get_client().search("Immunization", **params)
    return [(e["resource"]) for e in _entries(b)]


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
