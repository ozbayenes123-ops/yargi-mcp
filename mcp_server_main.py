# mcp_server_main.py

# --- MCP Spec Compliance: Reject null JSON-RPC IDs ---
# The mcp SDK's JSONRPCNotification uses extra="allow", which causes
# {"id": null} to be misclassified as a notification (202 Accepted).
# Per MCP 2025-11-25, null IDs must be rejected with -32600 Invalid Request.
# Changing to extra="forbid" makes validation fail for null IDs,
# returning a proper JSON-RPC error response.
from mcp.types import JSONRPCNotification as _McpJSONRPCNotification, JSONRPCMessage as _McpJSONRPCMessage
from pydantic import ConfigDict as _ConfigDict
_McpJSONRPCNotification.model_config = _ConfigDict(extra="forbid")
_McpJSONRPCNotification.model_rebuild(force=True)
_McpJSONRPCMessage.model_rebuild(force=True)
# --- End MCP Spec Compliance ---

import asyncio
import atexit
import logging
import httpx
import json
import os
import time
from collections import defaultdict
from pydantic import HttpUrl, Field
from typing import Optional, Dict, List, Literal, Any, Tuple, Annotated
from fastmcp.server.middleware import Middleware, MiddlewareContext

# Optional tiktoken import for token counting
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    tiktoken = None
from fastmcp import Context

# Use standard exception for tool errors
class ToolError(Exception):
    """Tool execution error"""
    pass

# --- Logging Configuration Start ---
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)
# --- Logging Configuration End ---

# --- Token Counting Middleware ---
class TokenCountingMiddleware(Middleware):
    """Middleware for counting input/output tokens using tiktoken."""
    
    def __init__(self, model: str = "cl100k_base"):
        """Initialize token counting middleware.

        Args:
            model: Tiktoken model name (cl100k_base for GPT-4/Claude compatibility)
        """
        if not TIKTOKEN_AVAILABLE:
            raise ImportError("tiktoken is required for token counting. Install with: pip install tiktoken")

        self.encoder = tiktoken.get_encoding(model)
        self.model = model
        self.token_stats = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0})
        self.logger = logging.getLogger("token_counter")
        self.logger.setLevel(logging.INFO)
    
    def count_tokens(self, text: str) -> int:
        """Count tokens in text using tiktoken."""
        if not text:
            return 0
        try:
            return len(self.encoder.encode(str(text)))
        except Exception as e:
            logger.warning(f"Token counting failed: {e}")
            return 0
    
    def extract_text_content(self, data: Any) -> str:
        """Extract text content from various data types."""
        if isinstance(data, str):
            return data
        elif isinstance(data, dict):
            # Extract text from common response fields
            text_parts = []
            for key, value in data.items():
                if isinstance(value, str):
                    text_parts.append(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            text_parts.append(item)
                        elif isinstance(item, dict) and 'text' in item:
                            text_parts.append(str(item['text']))
            return ' '.join(text_parts)
        elif isinstance(data, list):
            text_parts = []
            for item in data:
                text_parts.append(self.extract_text_content(item))
            return ' '.join(text_parts)
        else:
            return str(data)
    
    def log_token_usage(self, operation: str, input_tokens: int, output_tokens: int, 
                       tool_name: str = None, duration_ms: float = None):
        """Log token usage with structured format."""
        log_data = {
            "operation": operation,
            "tool_name": tool_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "duration_ms": duration_ms,
            "timestamp": time.time()
        }
        
        # Update statistics
        key = tool_name if tool_name else operation
        self.token_stats[key]["input"] += input_tokens
        self.token_stats[key]["output"] += output_tokens
        self.token_stats[key]["calls"] += 1
        
        # Log as JSON for easy parsing
        self.logger.info(json.dumps(log_data))
        
        # Also log human-readable format to main logger
        logger.info(f"Token Usage - {operation}" + 
                   (f" ({tool_name})" if tool_name else "") +
                   f": {input_tokens} in + {output_tokens} out = {input_tokens + output_tokens} total")
    
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        """Count tokens for tool calls."""
        start_time = time.perf_counter()
        
        # Extract tool name and arguments
        tool_name = getattr(context.message, 'name', 'unknown_tool')
        tool_args = getattr(context.message, 'arguments', {})
        
        # Count input tokens (tool arguments)
        input_text = self.extract_text_content(tool_args)
        input_tokens = self.count_tokens(input_text)
        
        try:
            # Execute the tool
            result = await call_next(context)
            
            # Count output tokens (tool result)
            output_text = self.extract_text_content(result)
            output_tokens = self.count_tokens(output_text)
            
            # Calculate duration
            duration_ms = (time.perf_counter() - start_time) * 1000
            
            # Log token usage
            self.log_token_usage("tool_call", input_tokens, output_tokens, 
                               tool_name, duration_ms)
            
            return result
            
        except Exception:
            duration_ms = (time.perf_counter() - start_time) * 1000
            self.log_token_usage("tool_call_error", input_tokens, 0, 
                               tool_name, duration_ms)
            raise
    
    async def on_read_resource(self, context: MiddlewareContext, call_next):
        """Count tokens for resource reads."""
        start_time = time.perf_counter()
        
        # Extract resource URI
        resource_uri = getattr(context.message, 'uri', 'unknown_resource')
        
        try:
            # Execute the resource read
            result = await call_next(context)
            
            # Count output tokens (resource content)
            output_text = self.extract_text_content(result)
            output_tokens = self.count_tokens(output_text)
            
            # Calculate duration
            duration_ms = (time.perf_counter() - start_time) * 1000
            
            # Log token usage (no input tokens for resource reads)
            self.log_token_usage("resource_read", 0, output_tokens, 
                               resource_uri, duration_ms)
            
            return result
            
        except Exception:
            duration_ms = (time.perf_counter() - start_time) * 1000
            self.log_token_usage("resource_read_error", 0, 0, 
                               resource_uri, duration_ms)
            raise
    
    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        """Count tokens for prompt retrievals."""
        start_time = time.perf_counter()
        
        # Extract prompt name
        prompt_name = getattr(context.message, 'name', 'unknown_prompt')
        
        try:
            # Execute the prompt retrieval
            result = await call_next(context)
            
            # Count output tokens (prompt content)
            output_text = self.extract_text_content(result)
            output_tokens = self.count_tokens(output_text)
            
            # Calculate duration
            duration_ms = (time.perf_counter() - start_time) * 1000
            
            # Log token usage
            self.log_token_usage("prompt_get", 0, output_tokens, 
                               prompt_name, duration_ms)
            
            return result
            
        except Exception:
            duration_ms = (time.perf_counter() - start_time) * 1000
            self.log_token_usage("prompt_get_error", 0, 0, 
                               prompt_name, duration_ms)
            raise
    
    def get_token_stats(self) -> Dict[str, Any]:
        """Get current token usage statistics."""
        return dict(self.token_stats)
    
    def reset_token_stats(self):
        """Reset token usage statistics."""
        self.token_stats.clear()

# --- End Token Counting Middleware ---

# Create FastMCP app directly without authentication wrapper
from fastmcp import FastMCP

def create_app():
    """Create FastMCP app with standard capabilities."""
    global app
    logger.info("MCP server created with standard capabilities...")
    
    # Add token counting middleware only if tiktoken is available
    if TIKTOKEN_AVAILABLE:
        try:
            token_counter = TokenCountingMiddleware()
            app.add_middleware(token_counter)
            logger.info("Token counting middleware added to MCP server")
        except Exception as e:
            logger.warning(f"Failed to add token counting middleware: {e}")
    
    return app

# --- Module Imports ---
from yargitay_mcp_module.client import YargitayOfficialApiClient
from bedesten_mcp_module.client import BedestenApiClient, BedestenRateLimited
from bedesten_mcp_module.models import (
    BedestenSearchRequest, BedestenSearchData,
    BedestenDocumentMarkdown, BedestenCourtTypeEnum
)
from bedesten_mcp_module.enums import BirimAdiEnum
from bedesten_mcp_module.query_parser import parse_search_query
from bedesten_mcp_module.citations import extract_citations

# Semantic Search Module Imports (enabled if any embedding provider is configured)
from semantic_search.embedder import is_semantic_search_available, is_local_embedding_configured
SEMANTIC_SEARCH_AVAILABLE = is_semantic_search_available()

if SEMANTIC_SEARCH_AVAILABLE:
    from semantic_search.embedder import get_embedder
    from semantic_search.vector_store import VectorStore
    from semantic_search.corpus import SemanticCorpus
    provider = "local" if is_local_embedding_configured() else "openrouter"
    logger.info(f"Semantic search enabled (provider={provider})")
else:
    logger.info("Semantic search disabled (no embedding provider configured)")

from danistay_mcp_module.client import DanistayApiClient
from emsal_mcp_module.client import EmsalApiClient
from emsal_mcp_module.models import (
    EmsalSearchRequest, CompactEmsalSearchResult
)
from uyusmazlik_mcp_module.client import UyusmazlikApiClient
from uyusmazlik_mcp_module.models import (
    UyusmazlikSearchRequest
)
from anayasa_mcp_module.client import AnayasaMahkemesiApiClient
from anayasa_mcp_module.bireysel_client import AnayasaBireyselBasvuruApiClient
from anayasa_mcp_module.unified_client import AnayasaUnifiedClient
from anayasa_mcp_module.models import (
    AnayasaUnifiedSearchRequest,
    # Removed enum imports - now using Literal strings in models
)
# KIK v2 Module Imports (New API)
from kik_mcp_module.client_v2 import KikV2ApiClient
from kik_mcp_module.models_v2 import KikV2DecisionType

from rekabet_mcp_module.client import RekabetKurumuApiClient
from rekabet_mcp_module.models import (
    RekabetKurumuSearchRequest,
    RekabetSearchResult,
    RekabetKararTuruGuidEnum
)

from sayistay_mcp_module.client import SayistayApiClient
from sayistay_mcp_module.models import (
    SayistayUnifiedSearchRequest
)
from sayistay_mcp_module.unified_client import SayistayUnifiedClient

# KVKK Module Imports
from kvkk_mcp_module.client import KvkkApiClient
from kvkk_mcp_module.models import (
    KvkkSearchRequest,
    KvkkSearchResult,
    KvkkDocumentMarkdown
)

# BDDK Module Imports
from bddk_mcp_module.client import BddkApiClient
from bddk_mcp_module.models import (
    BddkSearchRequest
)

# BTK Module Imports
from btk_mcp_module.client import BtkApiClient
from btk_mcp_module.models import (
    BtkDocumentMarkdown,
    BtkSearchRequest,
    BtkSearchResult
)

# GİB Module Imports
from gib_mcp_module.client import GibApiClient
from gib_mcp_module.models import (
    GibSearchRequest,
    GibSearchResult,
    GibDocumentMarkdown
)

# Sigorta Tahkim Module Imports
from sigorta_tahkim_mcp_module.client import SigortaTahkimApiClient
from sigorta_tahkim_mcp_module.models import (
    SigortaTahkimSearchRequest
)

# Mevzuat Module Imports (Bedesten legislation API)
from mevzuat_mcp_module.client import MevzuatApiClient
from mevzuat_mcp_module.models import (
    MevzuatSearchRequest
)

# Resmî Gazete Module Imports
from resmigazete_mcp_module.client import ResmiGazeteApiClient

# AİHM Module Imports (ECHR HUDOC)
from aihm_mcp_module.client import AihmApiClient
from aihm_mcp_module.models import (
    AihmSearchRequest
)

# KDK Module Imports (Kamu Denetçiliği Kurumu / Ombudsmanlık)
from kdk_mcp_module.client import KdkApiClient
from kdk_mcp_module.models import (
    KdkSearchRequest,
    KdkSearchResult,
    KdkDocumentMarkdown
)

# SPK Module Imports (Sermaye Piyasası Kurulu)
from spk_mcp_module.client import SpkApiClient
from spk_mcp_module.models import (
    SpkSearchRequest,
    SpkSearchResult,
    SpkBultenSummary,
    SpkDocumentMarkdown
)

# TBB Module Imports (Türkiye Barolar Birliği Disiplin Kurulu)
from tbb_mcp_module.client import TbbApiClient
from tbb_mcp_module.models import (
    TbbSearchRequest,
    TbbSearchResult,
    TbbDocumentMarkdown
)

# EPDK Module Imports (Enerji Piyasası Düzenleme Kurumu)
from epdk_mcp_module.client import EpdkApiClient
from epdk_mcp_module.models import (
    EpdkSearchRequest,
    EpdkSearchResult,
    EpdkDocumentMarkdown
)

# HSK Module Imports (Hâkimler ve Savcılar Kurulu İkinci Daire)
from hsk_mcp_module.client import HskApiClient
from hsk_mcp_module.models import (
    HskSearchRequest,
    HskSearchResult,
    HskDocumentMarkdown
)

# Reklam Kurulu Module Imports (Ticaret Bakanlığı)
from reklam_mcp_module.client import ReklamApiClient
from reklam_mcp_module.models import (
    ReklamBultenListe,
    ReklamBultenIciAramaSonucu,
    ReklamBultenMarkdown
)


# Create a placeholder app that will be properly initialized after tools are defined

# MCP app for Turkish legal databases with explicit capabilities
app = FastMCP(
    name="Yargı MCP Server",
    version="0.1.6"
)

# --- Health Check Functions (using individual clients) ---

# --- API Client Instances ---
yargitay_client_instance = YargitayOfficialApiClient()
danistay_client_instance = DanistayApiClient()
emsal_client_instance = EmsalApiClient()
uyusmazlik_client_instance = UyusmazlikApiClient()
anayasa_norm_client_instance = AnayasaMahkemesiApiClient()
anayasa_bireysel_client_instance = AnayasaBireyselBasvuruApiClient()
anayasa_unified_client_instance = AnayasaUnifiedClient()
kik_v2_client_instance = KikV2ApiClient()
rekabet_client_instance = RekabetKurumuApiClient()
bedesten_client_instance = BedestenApiClient()
sayistay_client_instance = SayistayApiClient()
sayistay_unified_client_instance = SayistayUnifiedClient()
kvkk_client_instance = KvkkApiClient()
kdk_client_instance = KdkApiClient()
spk_client_instance = SpkApiClient()
tbb_client_instance = TbbApiClient()
epdk_client_instance = EpdkApiClient()
hsk_client_instance = HskApiClient()
reklam_client_instance = ReklamApiClient()
bddk_client_instance = BddkApiClient()
btk_client_instance = BtkApiClient()
gib_client_instance = GibApiClient()
sigorta_tahkim_client_instance = SigortaTahkimApiClient()
mevzuat_client_instance = MevzuatApiClient()
resmi_gazete_client_instance = ResmiGazeteApiClient()
aihm_client_instance = AihmApiClient()

# Health check client (singleton for reuse)
_health_check_client: Optional[httpx.AsyncClient] = None


KARAR_TURU_ADI_TO_GUID_ENUM_MAP = {
    "": RekabetKararTuruGuidEnum.TUMU,  # Keep for backward compatibility
    "ALL": RekabetKararTuruGuidEnum.TUMU,  # Map "ALL" to TUMU
    "Birleşme ve Devralma": RekabetKararTuruGuidEnum.BIRLESME_DEVRALMA,
    "Diğer": RekabetKararTuruGuidEnum.DIGER,
    "Menfi Tespit ve Muafiyet": RekabetKararTuruGuidEnum.MENFI_TESPIT_MUAFIYET,
    "Özelleştirme": RekabetKararTuruGuidEnum.OZELLESTIRME,
    "Rekabet İhlali": RekabetKararTuruGuidEnum.REKABET_IHLALI,
}

# --- MCP Tools for Yargitay ---
"""
@app.tool(
    description="Use this when searching Turkish Court of Cassation (Yargıtay) decisions. Supports 52 chamber filtering and advanced operators (+required, -excluded, \"exact phrase\").",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_yargitay_detailed(
    arananKelime: str = Field("", description="Turkish search keyword. Supports +required -excluded \"exact phrase\" operators"),
    birimYrgKurulDaire: str = Field("ALL", description="Chamber selection (52 options: Civil/Criminal chambers, General Assemblies)"),
    esasYil: str = Field("", description="Case year for 'Esas No'."),
    esasIlkSiraNo: str = Field("", description="Starting sequence number for 'Esas No'."),
    esasSonSiraNo: str = Field("", description="Ending sequence number for 'Esas No'."),
    kararYil: str = Field("", description="Decision year for 'Karar No'."),
    kararIlkSiraNo: str = Field("", description="Starting sequence number for 'Karar No'."),
    kararSonSiraNo: str = Field("", description="Ending sequence number for 'Karar No'."),
    baslangicTarihi: str = Field("", description="Start date for decision search (DD.MM.YYYY)."),
    bitisTarihi: str = Field("", description="End date for decision search (DD.MM.YYYY)."),
    # pageSize: int = Field(10, ge=1, le=10, description="Number of results per page."),
    pageNumber: int = Field(1, ge=1, description="Page number to retrieve.")
) -> CompactYargitaySearchResult:
    # Search Yargıtay decisions using primary API with 52 chamber filtering and advanced operators.
    
    # Convert "ALL" to empty string for API compatibility
    if birimYrgKurulDaire == "ALL":
        birimYrgKurulDaire = ""
    
    pageSize = 10  # Default value
    
    search_query = YargitayDetailedSearchRequest(
        arananKelime=arananKelime,
        birimYrgKurulDaire=birimYrgKurulDaire,
        esasYil=esasYil,
        esasIlkSiraNo=esasIlkSiraNo,
        esasSonSiraNo=esasSonSiraNo,
        kararYil=kararYil,
        kararIlkSiraNo=kararIlkSiraNo,
        kararSonSiraNo=kararSonSiraNo,
        baslangicTarihi=baslangicTarihi,
        bitisTarihi=bitisTarihi,
        siralama="3",
        siralamaDirection="desc",
        pageSize=pageSize,
        pageNumber=pageNumber
    )
    
    logger.info(f"Tool 'search_yargitay_detailed' called: {search_query.model_dump_json(exclude_none=True, indent=2)}")
    try:
        api_response = await yargitay_client_instance.search_detailed_decisions(search_query)
        if api_response and api_response.data and api_response.data.data:
            # Convert to clean decision entries without arananKelime field
            clean_decisions = [
                CleanYargitayDecisionEntry(
                    id=decision.id,
                    daire=decision.daire,
                    esasNo=decision.esasNo,
                    kararNo=decision.kararNo,
                    kararTarihi=decision.kararTarihi,
                    document_url=decision.document_url
                )
                for decision in api_response.data.data
            ]
            return CompactYargitaySearchResult(
                decisions=clean_decisions,
                total_records=api_response.data.recordsTotal if api_response.data else 0,
                requested_page=search_query.pageNumber,
                page_size=search_query.pageSize)
        logger.warning("API response for Yargitay search did not contain expected data structure.")
        return CompactYargitaySearchResult(decisions=[], total_records=0, requested_page=search_query.pageNumber, page_size=search_query.pageSize)
    except Exception as e:
        logger.exception(f"Error in tool 'search_yargitay_detailed'.")
        raise

@app.tool(
    description="Use this when retrieving full text of a Yargıtay (Court of Cassation) decision. Returns clean Markdown format.",
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
async def get_yargitay_document_markdown(id: str) -> YargitayDocumentMarkdown:
    # Get Yargıtay decision text as Markdown. Use ID from search results.
    logger.info(f"Tool 'get_yargitay_document_markdown' called for ID: {id}")
    if not id or not id.strip(): raise ValueError("Document ID must be a non-empty string.")
    try:
        return await yargitay_client_instance.get_decision_document_as_markdown(id)
    except Exception as e:
        logger.exception(f"Error in tool 'get_yargitay_document_markdown'.")
        raise
"""

# --- MCP Tools for Danistay ---
"""
@app.tool(
    description="Use this when searching Turkish Council of State (Danıştay) decisions using AND/OR/NOT keyword logic.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_danistay_by_keyword(
    andKelimeler: List[str] = Field(default_factory=list, description="Keywords for AND logic, e.g., ['word1', 'word2']"),
    orKelimeler: List[str] = Field(default_factory=list, description="Keywords for OR logic."),
    notAndKelimeler: List[str] = Field(default_factory=list, description="Keywords for NOT AND logic."),
    notOrKelimeler: List[str] = Field(default_factory=list, description="Keywords for NOT OR logic."),
    pageNumber: int = Field(1, ge=1, description="Page number."),
    # pageSize: int = Field(10, ge=1, le=10, description="Results per page.")
) -> CompactDanistaySearchResult:
    # Search Danıştay decisions with keyword logic.
    
    pageSize = 10  # Default value
    
    search_query = DanistayKeywordSearchRequest(
        andKelimeler=andKelimeler,
        orKelimeler=orKelimeler,
        notAndKelimeler=notAndKelimeler,
        notOrKelimeler=notOrKelimeler,
        pageNumber=pageNumber,
        pageSize=pageSize
    )
    
    logger.info(f"Tool 'search_danistay_by_keyword' called.")
    try:
        api_response = await danistay_client_instance.search_keyword_decisions(search_query)
        if api_response.data:
            return CompactDanistaySearchResult(
                decisions=api_response.data.data,
                total_records=api_response.data.recordsTotal,
                requested_page=search_query.pageNumber,
                page_size=search_query.pageSize)
        logger.warning("API response for Danistay keyword search did not contain expected data structure.")
        return CompactDanistaySearchResult(decisions=[], total_records=0, requested_page=search_query.pageNumber, page_size=search_query.pageSize)
    except Exception as e:
        logger.exception(f"Error in tool 'search_danistay_by_keyword'.")
        raise

@app.tool(
    description="Use this when searching Danıştay decisions with specific chamber, case numbers, and date filters.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_danistay_detailed(
    daire: str = Field("", description="Chamber/Department name (e.g., '1. Daire')."),
    esasYil: str = Field("", description="Case year for 'Esas No'."),
    esasIlkSiraNo: str = Field("", description="Starting sequence for 'Esas No'."),
    esasSonSiraNo: str = Field("", description="Ending sequence for 'Esas No'."),
    kararYil: str = Field("", description="Decision year for 'Karar No'."),
    kararIlkSiraNo: str = Field("", description="Starting sequence for 'Karar No'."),
    kararSonSiraNo: str = Field("", description="Ending sequence for 'Karar No'."),
    baslangicTarihi: str = Field("", description="Start date for decision (DD.MM.YYYY)."),
    bitisTarihi: str = Field("", description="End date for decision (DD.MM.YYYY)."),
    mevzuatNumarasi: str = Field("", description="Legislation number."),
    mevzuatAdi: str = Field("", description="Legislation name."),
    madde: str = Field("", description="Article number."),
    pageNumber: int = Field(1, ge=1, description="Page number."),
    # pageSize: int = Field(10, ge=1, le=10, description="Results per page.")
) -> CompactDanistaySearchResult:
    # Search Danıştay decisions with detailed filtering.
    
    pageSize = 10  # Default value
    
    search_query = DanistayDetailedSearchRequest(
        daire=daire,
        esasYil=esasYil,
        esasIlkSiraNo=esasIlkSiraNo,
        esasSonSiraNo=esasSonSiraNo,
        kararYil=kararYil,
        kararIlkSiraNo=kararIlkSiraNo,
        kararSonSiraNo=kararSonSiraNo,
        baslangicTarihi=baslangicTarihi,
        bitisTarihi=bitisTarihi,
        mevzuatNumarasi=mevzuatNumarasi,
        mevzuatAdi=mevzuatAdi,
        madde=madde,
        siralama="3",
        siralamaDirection="desc",
        pageNumber=pageNumber,
        pageSize=pageSize
    )
    
    logger.info(f"Tool 'search_danistay_detailed' called.")
    try:
        api_response = await danistay_client_instance.search_detailed_decisions(search_query)
        if api_response.data:
            return CompactDanistaySearchResult(
                decisions=api_response.data.data,
                total_records=api_response.data.recordsTotal,
                requested_page=search_query.pageNumber,
                page_size=search_query.pageSize)
        logger.warning("API response for Danistay detailed search did not contain expected data structure.")
        return CompactDanistaySearchResult(decisions=[], total_records=0, requested_page=search_query.pageNumber, page_size=search_query.pageSize)
    except Exception as e:
        logger.exception(f"Error in tool 'search_danistay_detailed'.")
        raise

@app.tool(
    description="Use this when retrieving full text of a Danıştay (Council of State) decision. Returns clean Markdown format.",
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
async def get_danistay_document_markdown(id: str) -> DanistayDocumentMarkdown:
    # Get Danıştay decision text as Markdown. Use ID from search results.
    logger.info(f"Tool 'get_danistay_document_markdown' called for ID: {id}")
    if not id or not id.strip(): raise ValueError("Document ID must be a non-empty string for Danıştay.")
    try:
        return await danistay_client_instance.get_decision_document_as_markdown(id)
    except Exception as e:
        logger.exception(f"Error in tool 'get_danistay_document_markdown'.")
        raise
"""

# --- MCP Tools for Emsal ---
@app.tool(
    description="Use this when searching UYAP precedent decisions (Emsal). For lower court decisions and case law.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_emsal_detailed_decisions(
    keyword: str = Field("", description="Keyword to search."),
    selected_bam_civil_court: str = Field("", description="Selected BAM Civil Court."),
    selected_civil_court: str = Field("", description="Selected Civil Court."),
    selected_regional_civil_chambers: List[str] = Field(default_factory=list, description="Selected Regional Civil Chambers."),
    case_year_esas: str = Field("", description="Case year for 'Esas No'."),
    case_start_seq_esas: str = Field("", description="Starting sequence for 'Esas No'."),
    case_end_seq_esas: str = Field("", description="Ending sequence for 'Esas No'."),
    decision_year_karar: str = Field("", description="Decision year for 'Karar No'."),
    decision_start_seq_karar: str = Field("", description="Starting sequence for 'Karar No'."),
    decision_end_seq_karar: str = Field("", description="Ending sequence for 'Karar No'."),
    start_date: str = Field("", description="Start date for decision (DD.MM.YYYY)."),
    end_date: str = Field("", description="End date for decision (DD.MM.YYYY)."),
    sort_criteria: str = Field("1", description="Sorting criteria (e.g., 1: Esas No)."),
    sort_direction: str = Field("desc", description="Sorting direction ('asc' or 'desc')."),
    page_number: int = Field(1, ge=1, description="Page number (accepts int)."),
    # page_size: int = Field(10, ge=1, le=10, description="Results per page.")
) -> Dict[str, Any]:
    """Search Emsal precedent decisions with detailed criteria."""
    
    page_size = 10  # Default value
    
    search_query = EmsalSearchRequest(
        keyword=keyword,
        selected_bam_civil_court=selected_bam_civil_court,
        selected_civil_court=selected_civil_court,
        selected_regional_civil_chambers=selected_regional_civil_chambers,
        case_year_esas=case_year_esas,
        case_start_seq_esas=case_start_seq_esas,
        case_end_seq_esas=case_end_seq_esas,
        decision_year_karar=decision_year_karar,
        decision_start_seq_karar=decision_start_seq_karar,
        decision_end_seq_karar=decision_end_seq_karar,
        start_date=start_date,
        end_date=end_date,
        sort_criteria=sort_criteria,
        sort_direction=sort_direction,
        page_number=page_number,
        page_size=page_size
    )
    
    logger.info("Tool 'search_emsal_detailed_decisions' called.")
    try:
        api_response = await emsal_client_instance.search_detailed_decisions(search_query)
        if api_response.data:
            return CompactEmsalSearchResult(
                decisions=api_response.data.data,
                total_records=api_response.data.recordsTotal if api_response.data.recordsTotal is not None else 0,
                requested_page=search_query.page_number,
                page_size=search_query.page_size
            ).model_dump()
        logger.warning("API response for Emsal search did not contain expected data structure.")
        return CompactEmsalSearchResult(decisions=[], total_records=0, requested_page=search_query.page_number, page_size=search_query.page_size).model_dump()
    except Exception:
        logger.exception("Error in tool 'search_emsal_detailed_decisions'.")
        raise

@app.tool(
    description="Use this when retrieving full text of an Emsal precedent decision. Returns clean Markdown format.",
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
async def get_emsal_document_markdown(id: str) -> Dict[str, Any]:
    """Get document as Markdown."""
    logger.info(f"Tool 'get_emsal_document_markdown' called for ID: {id}")
    if not id or not id.strip(): raise ValueError("Document ID required for Emsal.")
    try:
        result = await emsal_client_instance.get_decision_document_as_markdown(id)
        return result.model_dump()
    except Exception:
        logger.exception("Error in tool 'get_emsal_document_markdown'.")
        raise

# --- MCP Tools for Uyusmazlik ---
@app.tool(
    description="Use this when searching jurisdictional dispute court (Uyuşmazlık Mahkemesi) decisions. Resolves conflicts between civil and administrative courts.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_uyusmazlik_decisions(
    icerik: str = Field("", description="Search text. Searches full decision text, or matches a case/decision number depending on search_scope."),
    search_scope: Literal["All", "EsasNo", "KararNo"] = Field("All", description="Search scope: 'All' (full text), 'EsasNo' (by case number), 'KararNo' (by decision number)."),
    case_sensitive: bool = Field(False, description="Whether the search is case sensitive."),
    page_number: int = Field(1, ge=1, description="Result page number.")
) -> Dict[str, Any]:
    """Search Court of Jurisdictional Disputes (Uyuşmazlık Mahkemesi) decisions."""

    search_params = UyusmazlikSearchRequest(
        icerik=icerik,
        search_scope=search_scope,
        case_sensitive=case_sensitive,
        page_number=page_number,
    )

    logger.info("Tool 'search_uyusmazlik_decisions' called.")
    try:
        result = await uyusmazlik_client_instance.search_decisions(search_params)
        return result.model_dump(mode="json")
    except Exception:
        logger.exception("Error in tool 'search_uyusmazlik_decisions'.")
        raise

@app.tool(
    description="Use this when retrieving full text of an Uyuşmazlık Mahkemesi decision. Returns clean Markdown format.",
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
async def get_uyusmazlik_document_markdown_from_url(
    document_url: str = Field(..., description="Full URL to the Uyuşmazlık Mahkemesi decision document from search results")
) -> Dict[str, Any]:
    """Get Uyuşmazlık Mahkemesi decision as Markdown."""
    logger.info(f"Tool 'get_uyusmazlik_document_markdown_from_url' called for URL: {str(document_url)}")
    if not document_url:
        raise ValueError("Document URL (document_url) is required for Uyuşmazlık document retrieval.")
    try:
        result = await uyusmazlik_client_instance.get_decision_document_as_markdown(str(document_url))
        return result.model_dump()
    except Exception:
        logger.exception("Error in tool 'get_uyusmazlik_document_markdown_from_url'.")
        raise

# --- DEACTIVATED: MCP Tools for Anayasa Mahkemesi (Individual Tools) ---
# Use search_anayasa_unified and get_anayasa_document_unified instead

"""
@app.tool(
    description="Use this when searching Turkish Constitutional Court norm control decisions. For constitutional review and legislation challenges.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
# DEACTIVATED TOOL - Use search_anayasa_unified instead
# @app.tool(
#     description="DEACTIVATED - Use search_anayasa_unified instead",
#     annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True}
# )
# async def search_anayasa_norm_denetimi_decisions(...) -> AnayasaSearchResult:
#     raise ValueError("This tool is deactivated. Use search_anayasa_unified instead.")

# DEACTIVATED TOOL - Use get_anayasa_document_unified instead
# @app.tool(...)
# async def get_anayasa_norm_denetimi_document_markdown(...) -> AnayasaDocumentMarkdown:
#     raise ValueError("This tool is deactivated. Use get_anayasa_document_unified instead.")

# DEACTIVATED TOOL - Use search_anayasa_unified instead
# @app.tool(...)
# async def search_anayasa_bireysel_basvuru_report(...) -> AnayasaBireyselReportSearchResult:
#     raise ValueError("This tool is deactivated. Use search_anayasa_unified instead.")

# DEACTIVATED TOOL - Use get_anayasa_document_unified instead
# @app.tool(...)
# async def get_anayasa_bireysel_basvuru_document_markdown(...) -> AnayasaBireyselBasvuruDocumentMarkdown:
#     raise ValueError("This tool is deactivated. Use get_anayasa_document_unified instead.")
"""

# --- Unified MCP Tools for Anayasa Mahkemesi ---
@app.tool(
    description=(
        "Use this when searching Turkish Constitutional Court decision records. Supports norm control decisions "
        "and individual application decisions. Norm control filters include reviewed norm metadata; results are court decisions."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_anayasa_unified(
    decision_type: Literal["norm_denetimi", "bireysel_basvuru"] = Field(..., description="Decision type: norm_denetimi (norm control) or bireysel_basvuru (individual applications)"),
    keywords: List[str] = Field(default_factory=list, description="Keywords for full-text search (joined into a single query)"),
    page_to_fetch: int = Field(1, ge=1, le=100, description="Page number to fetch (1-100)"),
    results_per_page: int = Field(10, ge=1, le=100, description="Results per page (1-100)")
) -> str:
    logger.info(f"Tool 'search_anayasa_unified' called for decision_type: {decision_type}")

    try:
        request = AnayasaUnifiedSearchRequest(
            decision_type=decision_type,
            keywords=keywords,
            page_to_fetch=page_to_fetch,
            results_per_page=results_per_page,
        )

        result = await anayasa_unified_client_instance.search_unified(request)
        return json.dumps(result.model_dump(), ensure_ascii=False, indent=2)

    except Exception:
        logger.exception("Error in tool 'search_anayasa_unified'.")
        raise

@app.tool(
    description="Use this when retrieving full text of a Constitutional Court decision. Auto-detects decision type from URL.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_anayasa_document_unified(
    document_url: str = Field(..., description="Document URL from search results"),
    page_number: int = Field(1, ge=1, description="Page number for paginated content (1-indexed)")
) -> str:
    logger.info(f"Tool 'get_anayasa_document_unified' called for URL: {document_url}, Page: {page_number}")
    
    try:
        result = await anayasa_unified_client_instance.get_document_unified(document_url, page_number)
        return json.dumps(result.model_dump(mode='json'), ensure_ascii=False, indent=2)
        
    except Exception:
        logger.exception("Error in tool 'get_anayasa_document_unified'.")
        raise

# --- MCP Tools for KIK v2 (Kamu İhale Kurulu - New API) ---
@app.tool(
    description="Use this when searching Turkish public procurement disputes (KİK). Supports dispute, regulatory, and court decision types.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_kik_v2_decisions(
    decision_type: str = Field("uyusmazlik", description="Decision type: 'uyusmazlik' (disputes), 'duzenleyici' (regulatory), or 'mahkeme' (court decisions)"),
    karar_metni: str = Field("", description="Decision text search query"),
    karar_no: str = Field("", description="Decision number (e.g., '2025/UH.II-1801')"),
    basvuran: str = Field("", description="Applicant name"),
    idare_adi: str = Field("", description="Administration/procuring entity name"),
    baslangic_tarihi: str = Field("", description="Start date (YYYY-MM-DD format, e.g., '2025-01-01')"),
    bitis_tarihi: str = Field("", description="End date (YYYY-MM-DD format, e.g., '2025-12-31')")
) -> dict:
    """Search Public Procurement Authority (KİK) decisions using the new v2 API.
    
    This tool supports all three KİK decision types:
    - uyusmazlik: Disputes and conflicts in public procurement
    - duzenleyici: Regulatory decisions and guidelines  
    - mahkeme: Court decisions and legal interpretations
    
    Each decision type uses its respective endpoint (GetKurulKararlari, GetKurulKararlariDk, GetKurulKararlariMk)
    and returns results with the decision_type field populated for identification.
    """
    
    logger.info(f"Tool 'search_kik_v2_decisions' called with decision_type='{decision_type}', karar_metni='{karar_metni}', karar_no='{karar_no}'")
    
    try:
        # Validate and convert decision type
        try:
            kik_decision_type = KikV2DecisionType(decision_type)
        except ValueError:
            return {
                "decisions": [],
                "total_records": 0,
                "page": 1,
                "error_code": "INVALID_DECISION_TYPE",
                "error_message": f"Invalid decision type: {decision_type}. Valid options: uyusmazlik, duzenleyici, mahkeme"
            }
        
        api_response = await kik_v2_client_instance.search_decisions(
            decision_type=kik_decision_type,
            karar_metni=karar_metni,
            karar_no=karar_no,
            basvuran=basvuran,
            idare_adi=idare_adi,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi
        )
        
        # Convert to dictionary for MCP tool response
        result = {
            "decisions": [decision.model_dump() for decision in api_response.decisions],
            "total_records": api_response.total_records,
            "page": api_response.page,
            "error_code": api_response.error_code,
            "error_message": api_response.error_message
        }
        
        logger.info(f"KİK v2 {decision_type} search completed. Found {len(api_response.decisions)} decisions")
        return result
        
    except Exception as e:
        logger.exception(f"Error in KİK v2 {decision_type} search tool 'search_kik_v2_decisions'.")
        return {
            "decisions": [],
            "total_records": 0,
            "page": 1,
            "error_code": "TOOL_ERROR",
            "error_message": str(e)
        }

@app.tool(
    description="Use this when retrieving full text of a KİK procurement decision. Returns document in Markdown format.",
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
async def get_kik_v2_document_markdown(
    gundemMaddesiId: str = Field(..., description="gundemMaddesiId from search_kik_v2_decisions results")
) -> dict:
    """Get KİK decision document in Markdown format."""

    logger.info(f"Tool 'get_kik_v2_document_markdown' called for gundemMaddesiId: {gundemMaddesiId}")

    if not gundemMaddesiId or not gundemMaddesiId.strip():
        return {
            "document_id": gundemMaddesiId,
            "kararNo": "",
            "markdown_content": "",
            "source_url": "",
            "error_message": "gundemMaddesiId is required and must be a non-empty string"
        }

    try:
        api_response = await kik_v2_client_instance.get_document_markdown(gundemMaddesiId)

        return {
            "document_id": api_response.document_id,
            "kararNo": api_response.kararNo,
            "markdown_content": api_response.markdown_content,
            "source_url": api_response.source_url,
            "error_message": api_response.error_message
        }

    except Exception as e:
        logger.exception(f"Error in KİK v2 document retrieval tool for gundemMaddesiId: {gundemMaddesiId}")
        return {
            "document_id": gundemMaddesiId,
            "kararNo": "",
            "markdown_content": "",
            "source_url": "",
            "error_message": f"Tool-level error during document retrieval: {str(e)}"
        }
@app.tool(
    description="Use this when searching Turkish competition law and antitrust decisions (Rekabet Kurumu).",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_rekabet_kurumu_decisions(
    sayfaAdi: str = Field("", description="Search in decision title (Başlık)."),
    YayinlanmaTarihi: str = Field("", description="Publication date (Yayım Tarihi), e.g., DD.MM.YYYY."),
    PdfText: str = Field(
        "",
        description='Search in decision text. Use "\\"kesin cümle\\"" for precise matching.'
    ),
    KararTuru: Literal[ 
        "ALL", 
        "Birleşme ve Devralma",
        "Diğer",
        "Menfi Tespit ve Muafiyet",
        "Özelleştirme",
        "Rekabet İhlali"
    ] = Field("ALL", description="Parameter description"),
    KararSayisi: str = Field("", description="Decision number (Karar Sayısı)."),
    KararTarihi: str = Field("", description="Decision date (Karar Tarihi), e.g., DD.MM.YYYY."),
    page: int = Field(1, ge=1, description="Page number to fetch for the results list.")
) -> Dict[str, Any]:
    """Search Competition Authority decisions."""
    
    karar_turu_guid_enum = KARAR_TURU_ADI_TO_GUID_ENUM_MAP.get(KararTuru)

    try:
        if karar_turu_guid_enum is None: 
            logger.warning(f"Invalid user-provided KararTuru: '{KararTuru}'. Defaulting to TUMU (all).")
            karar_turu_guid_enum = RekabetKararTuruGuidEnum.TUMU
    except Exception as e_map: 
        logger.error(f"Error mapping KararTuru '{KararTuru}': {e_map}. Defaulting to TUMU.")
        karar_turu_guid_enum = RekabetKararTuruGuidEnum.TUMU

    search_query = RekabetKurumuSearchRequest(
        sayfaAdi=sayfaAdi,
        YayinlanmaTarihi=YayinlanmaTarihi,
        PdfText=PdfText,
        KararTuruID=karar_turu_guid_enum, 
        KararSayisi=KararSayisi,
        KararTarihi=KararTarihi,
        page=page
    )
    logger.info(f"Tool 'search_rekabet_kurumu_decisions' called. Query: {search_query.model_dump_json(exclude_none=True, indent=2)}")
    try:
       
        result = await rekabet_client_instance.search_decisions(search_query)
        return result.model_dump()
    except Exception:
        logger.exception("Error in tool 'search_rekabet_kurumu_decisions'.")
        return RekabetSearchResult(decisions=[], retrieved_page_number=page, total_records_found=0, total_pages=0).model_dump()

@app.tool(
    description="Use this when retrieving full text of a Competition Authority decision. Returns paginated Markdown format.",
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
async def get_rekabet_kurumu_document(
    karar_id: str = Field(..., description="GUID (kararId) of the Rekabet Kurumu decision. This ID is obtained from search results."),
    page_number: int = Field(1, ge=1, description="Requested page number for the Markdown content converted from PDF (1-indexed, accepts int). Default is 1.")
) -> Dict[str, Any]:
    """Get Competition Authority decision as paginated Markdown."""
    logger.info(f"Tool 'get_rekabet_kurumu_document' called. Karar ID: {karar_id}, Markdown Page: {page_number}")
    
    current_page_to_fetch = page_number if page_number >= 1 else 1
    
    try:
        result = await rekabet_client_instance.get_decision_document(karar_id, page_number=current_page_to_fetch)
        return result.model_dump()
    except Exception:
        logger.exception(f"Error in tool 'get_rekabet_kurumu_document'. Karar ID: {karar_id}")
        raise 

# --- MCP Tools for Bedesten (Unified Search Across All Courts) ---
@app.tool(
    description=(
        "Use this for Turkish court decision records from Yargıtay, Danıştay, Local Courts, Appeals Courts, and KYB via Bedesten. "
        "Prefer narrow court_types over all courts. pageSize is intentionally fixed to 10 results per page. "
        "Bedesten is upstream rate-limited; avoid parallel repeated calls and wait retry_after seconds after 429 responses."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_bedesten_unified(
    ctx: Context,
    phrase: str = Field(..., description="""Search query in Turkish. SUPPORTED OPERATORS:
• Simple: "mülkiyet hakkı" (finds both words)
• Exact phrase: "\"mülkiyet hakkı\"" (finds exact phrase)  
• Required term: "+mülkiyet hakkı" (must contain mülkiyet)
• Exclude term: "mülkiyet -kira" (contains mülkiyet but not kira)
• Boolean AND: "mülkiyet AND hak" (both terms required)
• Boolean OR: "mülkiyet OR tapu" (either term acceptable)
• Boolean NOT: "mülkiyet NOT satış" (contains mülkiyet but not satış)
NOTE: Wildcards (*,?), regex patterns (/regex/), fuzzy search (~), and proximity search are NOT supported.
For best results, use exact phrases with quotes for legal terms."""),
    court_types: List[BedestenCourtTypeEnum] = Field(
        default=["YARGITAYKARARI", "DANISTAYKARAR"], 
        description="Court types: YARGITAYKARARI, DANISTAYKARAR, YERELHUKUK, ISTINAFHUKUK, KYB"
    ),
    # pageSize: int = Field(10, ge=1, le=10, description="Results per page (1-10)"),
    pageNumber: int = Field(1, ge=1, description="Page number. Each page returns 10 results; pageSize is fixed by the server."),
    birimAdi: BirimAdiEnum = Field("ALL", description="""
        Chamber filter (optional). Abbreviated values with Turkish names:
        • Yargıtay: H1-H23 (1-23. Hukuk Dairesi), C1-C23 (1-23. Ceza Dairesi), HGK (Hukuk Genel Kurulu), CGK (Ceza Genel Kurulu), BGK (Büyük Genel Kurulu), HBK (Hukuk Daireleri Başkanlar Kurulu), CBK (Ceza Daireleri Başkanlar Kurulu)
        • Danıştay: D1-D17 (1-17. Daire), DBGK (Büyük Gen.Kur.), IDDK (İdare Dava Daireleri Kurulu), VDDK (Vergi Dava Daireleri Kurulu), IBK (İçtihatları Birleştirme Kurulu), IIK (İdari İşler Kurulu), DBK (Başkanlar Kurulu), AYIM (Askeri Yüksek İdare Mahkemesi), AYIM1-3 (Askeri Yüksek İdare Mahkemesi 1-3. Daire)
        """),
    kararTarihiStart: str = Field("", description="Start date (ISO 8601 format)"),
    kararTarihiEnd: str = Field("", description="End date (ISO 8601 format)")
) -> dict:
    """Search Turkish legal databases via unified Bedesten API."""
    
    pageSize = 10  # Default value
    
    # Convert date formats if provided
    # Accept formats: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS.000Z
    if kararTarihiStart and not kararTarihiStart.endswith('Z'):
        # Convert simple date format to ISO 8601 with timezone
        if 'T' not in kararTarihiStart:
            kararTarihiStart = f"{kararTarihiStart}T00:00:00.000Z"
    
    if kararTarihiEnd and not kararTarihiEnd.endswith('Z'):
        # Convert simple date format to ISO 8601 with timezone
        if 'T' not in kararTarihiEnd:
            kararTarihiEnd = f"{kararTarihiEnd}T23:59:59.999Z"
    
    search_data = BedestenSearchData(
        pageSize=pageSize,
        pageNumber=pageNumber,
        itemTypeList=court_types,
        phrase=phrase,
        birimAdi=birimAdi,
        kararTarihiStart=kararTarihiStart,
        kararTarihiEnd=kararTarihiEnd
    )
    
    search_request = BedestenSearchRequest(data=search_data)
    
    logger.info(f"Searching bedesten: phrase='{phrase}', court_types={court_types}, birimAdi='{birimAdi}', page={pageNumber}")
    
    try:
        response = await bedesten_client_instance.search_documents(search_request)

        if response.data is None:
            return {
                "decisions": [],
                "total_records": 0,
                "requested_page": pageNumber,
                "page_size": pageSize,
                "searched_courts": court_types,
                "error": "No data returned from Bedesten API"
            }

        # Add null safety checks for response.data fields
        emsal_karar_list = response.data.emsalKararList if hasattr(response.data, 'emsalKararList') and response.data.emsalKararList is not None else []
        total_records = response.data.total if hasattr(response.data, 'total') and response.data.total is not None else 0

        return {
            "decisions": [d.model_dump() for d in emsal_karar_list],
            "total_records": total_records,
            "requested_page": pageNumber,
            "page_size": pageSize,
            "searched_courts": court_types
        }
    except BedestenRateLimited as e:
        retry_after = f"{e.retry_after:.1f}"
        logger.warning(f"Bedesten local rate-limit bucket full for search; retry-after={retry_after}s")
        return {
            "decisions": [],
            "total_records": 0,
            "requested_page": pageNumber,
            "page_size": pageSize,
            "searched_courts": court_types,
            "error": "rate_limit_exceeded",
            "status_code": 429,
            "retry_after": retry_after,
            "message": (
                "Bedesten istemci tarafı eşzamanlılık sınırına ulaşıldı "
                "(yerel token-bucket dolu). Lütfen kısa bir süre bekleyip "
                "aramayı tekrar deneyin. Yargı MCP'nin daha hızlı ve "
                "profesyonel versiyonunu test etmek için beta sürümüne "
                "kaydolabilirsiniz: "
                "https://yargi.betaspacestudio.com"
            ),
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            retry_after = e.response.headers.get("Retry-After", "")
            logger.warning(f"Bedesten API rate limit (429) for search; retry-after={retry_after!r}")
            return {
                "decisions": [],
                "total_records": 0,
                "requested_page": pageNumber,
                "page_size": pageSize,
                "searched_courts": court_types,
                "error": "rate_limit_exceeded",
                "status_code": 429,
                "retry_after": retry_after,
                "message": (
                    "Bedesten API rate limit aşıldı (HTTP 429 Too Many Requests). "
                    "Lütfen kısa bir süre bekleyip aramayı tekrar deneyin. "
                    "Yargı MCP'nin daha hızlı ve profesyonel versiyonunu test "
                    "etmek için beta sürümüne kaydolabilirsiniz: "
                    "https://yargi.betaspacestudio.com"
                ),
            }
        logger.exception("Error in tool 'search_bedesten_unified'")
        raise
    except Exception:
        logger.exception("Error in tool 'search_bedesten_unified'")
        raise

@app.tool(
    description=(
        "Use this when retrieving full text of a Bedesten search result by documentId. "
        "Counts against the same Bedesten upstream rate limit as search; after 429, wait retry_after seconds before retrying."
    ),
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
async def get_bedesten_document_markdown(
    documentId: str = Field(..., description="Document ID from Bedesten search results")
) -> BedestenDocumentMarkdown:
    """Get legal decision document as Markdown from Bedesten API."""
    logger.info(f"Tool 'get_bedesten_document_markdown' called for ID: {documentId}")
    
    if not documentId or not documentId.strip():
        raise ValueError("Document ID must be a non-empty string.")
    
    try:
        return await bedesten_client_instance.get_document_as_markdown(documentId)
    except BedestenRateLimited as e:
        retry_after = f"{e.retry_after:.1f}"
        logger.warning(f"Bedesten local rate-limit bucket full for document {documentId}; retry-after={retry_after}s")
        message = (
            "Bedesten istemci tarafı eşzamanlılık sınırına ulaşıldı "
            "(yerel token-bucket dolu). Lütfen kısa bir süre bekleyip "
            "belgeyi tekrar talep edin. Yargı MCP'nin daha hızlı ve "
            "profesyonel versiyonunu test etmek için beta sürümüne "
            "kaydolabilirsiniz: "
            "https://yargi.betaspacestudio.com "
            f"Retry-After: {retry_after}"
        )
        return BedestenDocumentMarkdown(
            documentId=documentId,
            markdown_content=f"ERROR (rate_limit_exceeded, HTTP 429): {message}",
            source_url=f"https://mevzuat.adalet.gov.tr/ictihat/{documentId}",
            mime_type=None,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            retry_after = e.response.headers.get("Retry-After", "")
            logger.warning(f"Bedesten API rate limit (429) for document {documentId}; retry-after={retry_after!r}")
            message = (
                "Bedesten API rate limit aşıldı (HTTP 429 Too Many Requests). "
                "Lütfen kısa bir süre bekleyip belgeyi tekrar talep edin. "
                "Yargı MCP'nin daha hızlı ve profesyonel versiyonunu test "
                "etmek için beta sürümüne kaydolabilirsiniz: "
                "https://yargi.betaspacestudio.com"
            )
            if retry_after:
                message += f" Retry-After: {retry_after}"
            return BedestenDocumentMarkdown(
                documentId=documentId,
                markdown_content=f"ERROR (rate_limit_exceeded, HTTP 429): {message}",
                source_url=f"https://mevzuat.adalet.gov.tr/ictihat/{documentId}",
                mime_type=None,
            )
        logger.exception("Error in tool 'get_bedesten_document_markdown'")
        raise
    except Exception:
        logger.exception("Error in tool 'get_bedesten_document_markdown'")
        raise


async def _run_ictihat_arama(
    sorgu: str,
    sayfa: int = 1,
    mahkemeler: str = "",
    daire: str = "",
    baslangic_tarihi: str = "",
    bitis_tarihi: str = "",
    page_size: int = 10,
) -> Dict[str, Any]:
    """
    Shared core for ictihat_ara / ictihat_semantik_ara: parses the query,
    builds per-court Bedesten requests, runs them in parallel, and merges
    results. Returns the standard result dict (or a rate-limit error dict).
    """
    logger.info("_run_ictihat_arama called with sorgu=%r page_size=%d", sorgu, page_size)

    parsed = parse_search_query(sorgu)

    # Explicit parameters override auto-detected ones
    if mahkemeler.strip():
        courts = [c.strip().upper() for c in mahkemeler.split(",") if c.strip()]
        courts = [c for c in courts if c in BedestenCourtTypeEnum.__args__]
        if courts:
            parsed.court_types = courts
    if daire.strip():
        parsed.birim_adi = daire.strip().upper()
    if baslangic_tarihi.strip():
        parsed.karar_tarihi_start = baslangic_tarihi.strip()
    if bitis_tarihi.strip():
        parsed.karar_tarihi_end = bitis_tarihi.strip()

    phrase = parsed.phrase or sorgu.strip()

    requests = []
    for court in parsed.court_types:
        requests.append(
            BedestenSearchRequest(
                data=BedestenSearchData(
                    pageSize=page_size,
                    pageNumber=sayfa,
                    itemTypeList=[court],
                    phrase=phrase,
                    birimAdi=parsed.birim_adi,
                    kararTarihiStart=parsed.karar_tarihi_start or None,
                    kararTarihiEnd=parsed.karar_tarihi_end or None,
                )
            )
        )

    try:
        responses = await bedesten_client_instance.search_documents_multi(requests)
    except BedestenRateLimited as e:
        logger.warning("_run_ictihat_arama: Bedesten local bucket full; retry-after=%.1fs", e.retry_after)
        return {
            "analiz": parsed.to_dict(),
            "decisions": [],
            "total_records": 0,
            "page": sayfa,
            "page_size": page_size,
            "error": "rate_limit_exceeded",
            "status_code": 429,
            "retry_after": f"{e.retry_after:.1f}",
            "message": "Bedesten istemci tarafı eşzamanlılık sınırına ulaşıldı; kısa bir süre sonra tekrar deneyin.",
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            retry_after = e.response.headers.get("Retry-After", "")
            logger.warning("_run_ictihat_arama: Bedesten API 429; retry-after=%r", retry_after)
            return {
                "analiz": parsed.to_dict(),
                "decisions": [],
                "total_records": 0,
                "page": sayfa,
                "page_size": page_size,
                "error": "rate_limit_exceeded",
                "status_code": 429,
                "retry_after": retry_after,
                "message": "Bedesten API rate limit aşıldı (HTTP 429); kısa bir süre sonra tekrar deneyin.",
            }
        logger.exception("_run_ictihat_arama error")
        raise

    merged = []
    total = 0
    seen: set = set()
    for response, court in zip(responses, parsed.court_types):
        if response is None or response.data is None:
            continue
        entries = response.data.emsalKararList or []
        total += response.data.total or 0
        for entry in entries:
            if entry.documentId in seen:
                continue
            seen.add(entry.documentId)
            merged.append({
                "documentId": entry.documentId,
                "court_type": court,
                "birimAdi": entry.birimAdi,
                "esasNo": entry.esasNo,
                "kararNo": entry.kararNo,
                "kararTarihi": entry.kararTarihi,
                "kararTarihiStr": entry.kararTarihiStr,
                "source_url": f"https://mevzuat.adalet.gov.tr/ictihat/{entry.documentId}",
            })

    merged.sort(key=lambda x: str(x.get("kararTarihi") or ""), reverse=True)

    sources = ", ".join(parsed.court_types) or "varsayılan"
    return {
        "ozet": f"'{phrase}' sorgusu {sources} kaynaklarında arandı; {total} kayıt bulundu, {len(merged)} karar döndürüldü.",
        "analiz": parsed.to_dict(),
        "decisions": merged,
        "total_records": total,
        "page": sayfa,
        "page_size": page_size,
    }


# --- İçtihat Arama (Akıllı Sorgu Analizi + Paralel Çoklu Mahkeme) ---

@app.tool(
    description=(
        "Smart unified search across Turkish case-law (içtihat) databases via the Bedesten "
        "API. Accepts a single free-form Turkish query and automatically extracts: court "
        "type (Yargıtay/Danıştay/istinaf/yerel/KYB), chamber (e.g. '1. Hukuk Dairesi' -> H1), "
        "judgment year or date range, and case numbers (esas/karar no). When multiple courts "
        "match, they are searched in parallel and results are merged. Use get_bedesten_document_markdown "
        "or ictihat_getir with a returned documentId for the full text."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def ictihat_ara(
    sorgu: str = Field(..., description="""Free-form Turkish search query. Examples:
• "Yargıtay 1. Hukuk Dairesi mülkiyet hakkı 2023" (court + chamber + year)
• "E.2023/1234 K.2024/567" (case numbers)
• "istinaf \"sözleşme ihlali\"" (court + exact phrase)
Supported operators: "exact phrase", +required, -exclude, AND/OR/NOT."""),
    sayfa: int = Field(1, ge=1, description="Page number. Each page returns 10 results per merged list."),
    mahkemeler: str = Field("", description="Optional explicit court filter (comma-separated): YARGITAYKARARI, DANISTAYKARAR, YERELHUKUK, ISTINAFHUKUK, KYB. Overrides auto-detection."),
    daire: str = Field("", description="Optional explicit chamber (e.g. 'H1', 'D3', 'HGK', 'VDDK'). Overrides auto-detection."),
    baslangic_tarihi: str = Field("", description="Optional start date YYYY-MM-DD. Overrides auto-detected range."),
    bitis_tarihi: str = Field("", description="Optional end date YYYY-MM-DD. Overrides auto-detected range.")
) -> Dict[str, Any]:
    """Smart unified case-law search with natural-language query parsing."""
    logger.info("ictihat_ara called with sorgu=%r", sorgu)
    return await _run_ictihat_arama(
        sorgu,
        sayfa=sayfa,
        mahkemeler=mahkemeler,
        daire=daire,
        baslangic_tarihi=baslangic_tarihi,
        bitis_tarihi=bitis_tarihi,
        page_size=10,
    )


@app.tool(
    description=(
        "Retrieve the full text of a Turkish court decision as Markdown with "
        "auto-extracted metadata: summary, case numbers, chamber, outcome (hüküm), "
        "and cited case numbers. Takes a documentId from ictihat_ara / search_bedesten_unified."
    ),
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
async def ictihat_getir(
    documentId: str = Field(..., description="Document ID from ictihat_ara / search_bedesten_unified results (or a mevzuat.adalet.gov.tr/ictihat/... URL)."),
    ozet_uzunlugu: int = Field(600, ge=100, le=3000, description="Length of the auto-extracted summary in characters.")
) -> Dict[str, Any]:
    """Smart document retrieval with automatic metadata enrichment."""
    logger.info("ictihat_getir called with documentId=%s", documentId)

    if not documentId or not documentId.strip():
        return {"documentId": documentId, "error_message": "documentId is required."}

    # Accept full URLs too: https://mevzuat.adalet.gov.tr/ictihat/{id}
    doc_id = documentId.strip()
    if doc_id.startswith("http"):
        doc_id = doc_id.rstrip("/").split("/")[-1]

    try:
        result = await bedesten_client_instance.get_document_as_markdown(doc_id)
    except BedestenRateLimited as e:
        return {
            "documentId": doc_id,
            "error_message": f"rate_limit_exceeded (HTTP 429), retry_after={e.retry_after:.1f}s",
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            return {
                "documentId": doc_id,
                "error_message": f"rate_limit_exceeded (HTTP 429), retry_after={e.response.headers.get('Retry-After', '')}",
            }
        logger.exception("ictihat_getir error")
        raise

    content = result.markdown_content or ""
    enrichment = _extract_decision_metadata(content, ozet_uzunlugu)

    return {
        "documentId": result.documentId,
        "source_url": result.source_url,
        "mime_type": result.mime_type,
        "markdown_content": content,
        "ozet": enrichment["ozet"],
        "esas_no": enrichment["esas_no"],
        "karar_no": enrichment["karar_no"],
        "mahkeme": enrichment["mahkeme"],
        "hukum_ozeti": enrichment["hukum"],
        "atif_yapilan_kararlar": enrichment["atıflar"],
        "error_message": None,
    }


# --- Atıf Zinciri (Precedent Chain) ---

@app.tool(
    description=(
        "Follows the precedent chain (atıf zinciri) of a Turkish court decision: "
        "fetches the decision's full text, extracts the case-law citations "
        "(esas/karar numbers), and resolves each cited decision against Bedesten, "
        "returning documentIds so the chain can be continued recursively. Optionally "
        "also finds incoming citations (decisions that cite the given decision). "
        "WARNING: each citation resolution costs one Bedesten search; a full run "
        "takes ~maks_atif+1 searches."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def karar_atif_zinciri(
    documentId: str = Field(..., description="Document ID from ictihat_ara / ictihat_semantik_ara / search_bedesten_unified results (or a mevzuat.adalet.gov.tr/ictihat/... URL)."),
    maks_atif: int = Field(8, ge=1, le=20, description="Maximum number of distinct citations to resolve (default 8; each costs one rate-limited Bedesten search)."),
    eslesme_limiti: int = Field(3, ge=1, le=5, description="Maximum candidate matches to return per resolved citation."),
    atif_alanlari_ara: bool = Field(True, description="Also search for decisions that may cite the given decision (matched by its karar no)."),
) -> Dict[str, Any]:
    """Follow the citation chain of a decision forward (and optionally backward)."""
    logger.info("karar_atif_zinciri called with documentId=%s", documentId)

    if not documentId or not documentId.strip():
        return {"documentId": documentId, "error_message": "documentId is required."}

    doc_id = documentId.strip()
    if doc_id.startswith("http"):
        doc_id = doc_id.rstrip("/").split("/")[-1]

    try:
        result = await bedesten_client_instance.get_document_as_markdown(doc_id)
    except BedestenRateLimited as e:
        return {
            "documentId": doc_id,
            "error_message": f"rate_limit_exceeded (HTTP 429), retry_after={e.retry_after:.1f}s",
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            return {
                "documentId": doc_id,
                "error_message": f"rate_limit_exceeded (HTTP 429), retry_after={e.response.headers.get('Retry-After', '')}",
            }
        logger.exception("karar_atif_zinciri: document fetch failed")
        raise

    content = result.markdown_content or ""
    meta = _extract_decision_metadata(content, 600)
    own_numbers = [(meta["esas_no"], meta["karar_no"])] if (meta["esas_no"] or meta["karar_no"]) else []

    citations = extract_citations(content, own_numbers=own_numbers, maks_atif=maks_atif)
    logger.info("karar_atif_zinciri: extracted %d citations from %s", len(citations), doc_id)

    # Resolve each citation against Bedesten (Yargıtay + Danıştay precedents)
    requests = []
    for c in citations:
        num = c["karar_no"] or c["esas_no"]
        requests.append(
            BedestenSearchRequest(
                data=BedestenSearchData(
                    phrase=f'"{num}"',
                    itemTypeList=["YARGITAYKARARI", "DANISTAYKARAR"],
                    pageSize=eslesme_limiti,
                    pageNumber=1,
                )
            )
        )

    resolved = 0
    if requests:
        responses = await bedesten_client_instance.search_documents_multi(requests)
        for c, resp in zip(citations, responses):
            matches = []
            if resp is not None and resp.data and resp.data.emsalKararList:
                for entry in resp.data.emsalKararList[:eslesme_limiti]:
                    matches.append({
                        "documentId": entry.documentId,
                        "birim_adi": entry.birimAdi,
                        "esas_no": entry.esasNo,
                        "karar_no": entry.kararNo,
                        "karar_tarihi": entry.kararTarihiStr,
                        "source_url": f"https://mevzuat.adalet.gov.tr/ictihat/{entry.documentId}",
                    })
                if matches:
                    resolved += 1
            c["eslesme"] = matches

    # Incoming citations: decisions whose text mentions this decision's karar no
    atif_alanlar = []
    own_num = meta["karar_no"] or meta["esas_no"]
    if atif_alanlari_ara and own_num:
        try:
            resp = await bedesten_client_instance.search_documents(
                BedestenSearchRequest(
                    data=BedestenSearchData(
                        phrase=f'"{own_num}"',
                        itemTypeList=["YARGITAYKARARI", "DANISTAYKARAR", "ISTINAFHUKUK", "YERELHUKUK"],
                        pageSize=eslesme_limiti,
                        pageNumber=1,
                    )
                )
            )
            if resp.data and resp.data.emsalKararList:
                for entry in resp.data.emsalKararList[:eslesme_limiti]:
                    if entry.documentId == doc_id:
                        continue
                    atif_alanlar.append({
                        "documentId": entry.documentId,
                        "birim_adi": entry.birimAdi,
                        "esas_no": entry.esasNo,
                        "karar_no": entry.kararNo,
                        "karar_tarihi": entry.kararTarihiStr,
                        "source_url": f"https://mevzuat.adalet.gov.tr/ictihat/{entry.documentId}",
                    })
        except BedestenRateLimited as e:
            logger.warning("karar_atif_zinciri: incoming-citation search rate limited (retry_after=%.1fs)", e.retry_after)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:
                logger.exception("karar_atif_zinciri: incoming-citation search failed")

    return {
        "documentId": doc_id,
        "source_url": result.source_url,
        "esas_no": meta["esas_no"],
        "karar_no": meta["karar_no"],
        "mahkeme": meta["mahkeme"],
        "toplam_ayrinti": len(citations),
        "cozulen": resolved,
        "atiflar": citations,
        "atif_alanlar": atif_alanlar,
        "message": "Her atıf için eşleşmeler Bedesten'de arandı; devam zinciri için eslesme[].documentId değerlerini kullanın.",
        "error_message": None,
    }


def _extract_decision_metadata(text: str, summary_length: int) -> Dict[str, Any]:
    """Extract özet, case numbers, court, outcome and cited decisions from a
    decision text (plain text produced by markdown conversion)."""
    import re as _re

    summary = ""
    mahkeme = ""
    hukum = ""

    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    # Summary: skip boilerplate header lines, take first meaningful paragraph
    body_start = 0
    for i, ln in enumerate(lines[:40]):
        low = ln.lower()
        if _re.search(r"^t\.c\.|^recep tayyip|^cumhurbaşkanı|^yargıtay|^danıştay|^avukat|^dosya", low):
            continue
        if _re.search(r"(^|\s)(\d{1,2}\.\s*)?(hukuk|ceza)\s+daire", low) or "genel kurul" in low:
            mahkeme = ln
        if len(ln) > 30:
            body_start = i
            break

    # Chamber line as the originating court if not yet found
    if not mahkeme:
        for ln in lines[:60]:
            low = ln.lower()
            if _re.search(r"(^|\s)(\d{1,2}\.)?\s*(hukuk|ceza|daire|kurul)", low) and len(ln) < 120:
                mahkeme = ln
                break

    body = " ".join(lines[body_start:]) if body_start < len(lines) else " ".join(lines)
    body = _re.sub(r"\s+", " ", body).strip()
    if body:
        summary = body[:summary_length].rsplit(" ", 1)[0]

    # Esas / Karar numbers (both orders: "E. 2022/6819" and "2022/6819 E.")
    # Prefer explicit header forms ("Esas No: ..."), then generic E./K.
    # patterns limited to the document header region so that case-law
    # citations in the body are not mistaken for the decision's own numbers.
    esas_no = ""
    karar_no = ""
    header = text[:800]
    m = _re.search(r"\b(?:esas no\s*[:.]?\s*)(\d{4}/\d{1,6})", text, _re.I)
    if m:
        esas_no = m.group(1)
    m = _re.search(r"\b(?:karar no\s*[:.]?\s*)(\d{4}/\d{1,6})", text, _re.I)
    if m:
        karar_no = m.group(1)
    if not esas_no:
        m = _re.search(r"\bE\s*[:.]?\s*(\d{4}/\d{1,6})\b|\b(\d{4}/\d{1,6})\s*E\.?", header)
        if m:
            esas_no = m.group(1) or m.group(2) or ""
    if not karar_no:
        m = _re.search(r"\bK\s*[:.]?\s*(\d{4}/\d{1,6})\b|\b(\d{4}/\d{1,6})\s*K\.?", header)
        if m:
            karar_no = m.group(1) or m.group(2) or ""

    # Outcome (hüküm / sonuç section)
    for marker in ("SONUÇ", "HÜKÜM", "GEREKÇE VE SONUÇ", "TEBLİĞ", "KARAR"):
        idx = text.find(marker)
        if idx != -1:
            snippet = _re.sub(r"\s+", " ", text[idx : idx + 1200]).strip()
            snippet = snippet[:summary_length].rsplit(" ", 1)[0]
            hukum = f"[{marker}] {snippet}"
            break

    # Cited case numbers (atıf yapılan kararlar)
    atiflar = []
    for num in _re.findall(r"\b(\d{4}/\d{1,6})\b", text):
        if num not in atiflar:
            atiflar.append(num)
    atiflar = atiflar[:50]

    return {
        "ozet": summary,
        "esas_no": esas_no,
        "karar_no": karar_no,
        "mahkeme": mahkeme,
        "hukum": hukum,
        "atıflar": atiflar,
    }


# --- Hibrit Semantik Arama (Kalıcı Vektör Korpusu) ---

def _decision_metadata_text(item: Dict[str, Any]) -> str:
    """Fallback text for a decision that has no fetched full text yet."""
    it = item.get("itemType")
    court_name = it.get("name") if isinstance(it, dict) else ""
    return " ".join(
        str(x) for x in (
            court_name,
            item.get("birimAdi"),
            item.get("esasNo"),
            item.get("kararNo"),
            item.get("kararTarihiStr") or item.get("kararTarihi"),
            item.get("kararTuru"),
        ) if x
    )


async def _fetch_full_texts(client: Any, targets: List[Dict[str, Any]], max_chars: int) -> Dict[str, str]:
    """Fetch full decision texts in parallel (bounded concurrency), truncated to max_chars."""
    sem = asyncio.Semaphore(3)

    async def _fetch(item: Dict[str, Any]):
        async with sem:
            try:
                result = await client.get_document_as_markdown(item["documentId"])
                content = (result.markdown_content or "").strip()
                return item["documentId"], content[:max_chars]
            except Exception:
                logger.warning("full-text fetch failed for %s", item["documentId"], exc_info=False)
                return item["documentId"], None

    pairs = await asyncio.gather(*[_fetch(i) for i in targets])
    return {d_id: content for d_id, content in pairs if content}


async def _index_new_decisions(
    corpus: "SemanticCorpus",
    embedder: Any,
    candidates: List[Dict[str, Any]],
    belge_sayisi: int,
    max_chars: int,
) -> int:
    """Index candidates not yet in the persistent corpus. Full texts are fetched for
    up to ``belge_sayisi`` of them; the rest are embedded from their search metadata.
    Returns the number of newly indexed documents."""
    new_candidates = [d for d in candidates if not corpus.has(d["documentId"])]
    if not new_candidates:
        return 0
    targets = new_candidates[:belge_sayisi] if belge_sayisi > 0 else []
    fetched = await _fetch_full_texts(bedesten_client_instance, targets, max_chars)
    texts = [fetched.get(i["documentId"]) or _decision_metadata_text(i) for i in new_candidates]
    embeddings = embedder.encode_documents(texts)
    return corpus.add_documents(
        ids=[i["documentId"] for i in new_candidates],
        texts=texts,
        embeddings=embeddings,
        metadata=[i for i in new_candidates],
    )


def _keyword_position_scores(decisions: List[Dict[str, Any]]) -> Dict[str, float]:
    return {
        item["documentId"]: 1.0 - i / max(1, len(decisions))
        for i, item in enumerate(decisions)
    }


def _hybrid_rank(
    corpus: "SemanticCorpus",
    query_embedding: Any,
    position: Dict[str, float],
    alpha: float,
    top_k: int,
) -> List[Tuple[Any, float, float, float]]:
    """Combine corpus vector similarity with keyword position scores."""
    vector_results = corpus.search(query_embedding, top_k=corpus.size)
    ranked = []
    for doc, vec_score in vector_results:
        kw = position.get(doc.id, 0.0)
        ranked.append((doc, vec_score, kw, alpha * vec_score + (1 - alpha) * kw))
    ranked.sort(key=lambda x: x[3], reverse=True)
    return ranked[:top_k]

if SEMANTIC_SEARCH_AVAILABLE:
    @app.tool(
        description=(
            "Hybrid semantic search across Turkish case-law: runs the same smart "
            "keyword search as ictihat_ara, then re-ranks the candidates with AI "
            "embeddings. Full decision texts are fetched once per document and "
            "indexed into a persistent on-disk vector corpus (SEMANTIC_INDEX_DIR, "
            "default data/semantic_index) so the corpus grows with every search and "
            "repeated queries become faster. A local, Pinecone-free alternative to "
            "hosted vector databases."
        ),
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True
        }
    )
    async def ictihat_semantik_ara(
        sorgu: str = Field(..., description="""Free-form Turkish search query (same syntax and auto-detection as ictihat_ara). Examples:
• "Yargıtay 1. Hukuk Dairesi mülkiyet hakkı 2023"
• "E.2023/1234 K.2024/567"
TIP: For best semantic ranking, phrase the query as a full sentence describing the legal issue (e.g. "Mirasçının muvazaalı satış işlemine karşı tapu iptali ve tescil davası açması")."""),
        mahkemeler: str = Field("", description="Optional explicit court filter (comma-separated): YARGITAYKARARI, DANISTAYKARAR, YERELHUKUK, ISTINAFHUKUK, KYB. Overrides auto-detection."),
        daire: str = Field("", description="Optional explicit chamber (e.g. 'H1', 'D3', 'HGK', 'VDDK'). Overrides auto-detection."),
        baslangic_tarihi: str = Field("", description="Optional start date YYYY-MM-DD. Overrides auto-detected range."),
        bitis_tarihi: str = Field("", description="Optional end date YYYY-MM-DD. Overrides auto-detected range."),
        top_k: int = Field(10, ge=1, le=50, description="Number of top results to return (1-50)."),
        kaynak_sayisi: int = Field(40, ge=10, le=100, description="How many keyword-search candidates to collect before semantic re-ranking (distributed across matched courts)."),
        belge_sayisi: int = Field(10, ge=0, le=25, description="How many candidate full texts to fetch and index into the persistent corpus (0 = index search metadata only). Only documents not already in the corpus are fetched."),
        alpha: float = Field(0.6, ge=0.0, le=1.0, description="Weight of semantic similarity vs keyword relevance (0 = pure keyword, 1 = pure semantic).")
    ) -> Dict[str, Any]:
        """Hybrid semantic case-law search with a persistent local vector corpus."""
        logger.info("ictihat_semantik_ara called with sorgu=%r", sorgu)

        if not sorgu or not sorgu.strip():
            return {"sorgu": sorgu, "error_message": "sorgu is required.", "results": []}

        parsed = parse_search_query(sorgu)
        court_count = max(1, len(parsed.court_types))
        core = await _run_ictihat_arama(
            sorgu,
            sayfa=1,
            mahkemeler=",".join(parsed.court_types),
            daire=daire,
            baslangic_tarihi=baslangic_tarihi,
            bitis_tarihi=bitis_tarihi,
            page_size=max(10, kaynak_sayisi // court_count),
        )

        if core.get("error") == "rate_limit_exceeded":
            core["sorgu"] = sorgu
            core["results"] = []
            return core

        decisions = (core.get("decisions") or [])[:kaynak_sayisi]
        if not decisions:
            return {
                "sorgu": sorgu,
                "analiz": core.get("analiz"),
                "results": [],
                "message": "Keyword search returned no documents.",
            }

        try:
            embedder = get_embedder()
        except Exception as e:
            logger.exception("ictihat_semantik_ara: embedder init failed")
            return {
                "sorgu": sorgu,
                "analiz": core.get("analiz"),
                "results": [],
                "error_message": f"Embedding provider unavailable: {e}",
            }

        corpus = SemanticCorpus(model=embedder.model, dimension=embedder.dimension)
        max_chars = int(os.getenv("SEMANTIC_MAX_CHARS", "2000"))

        added = await _index_new_decisions(corpus, embedder, decisions, belge_sayisi, max_chars)

        # Re-rank the corpus with the semantic query, blending in keyword position.
        query_embedding = embedder.encode_query(sorgu)
        ranked = _hybrid_rank(
            corpus, query_embedding,
            _keyword_position_scores(decisions), alpha, top_k,
        )

        results = []
        for doc, vec, kw, combined in ranked:
            meta = doc.metadata
            results.append({
                "documentId": doc.id,
                "court_type": meta.get("court_type"),
                "birim_adi": meta.get("birimAdi"),
                "esas_no": meta.get("esasNo"),
                "karar_no": meta.get("kararNo"),
                "karar_tarihi": meta.get("kararTarihiStr") or meta.get("kararTarihi"),
                "semantic_score": round(float(vec), 4),
                "keyword_score": round(float(kw), 4),
                "combined_score": round(float(combined), 4),
            })

        return {
            "sorgu": sorgu,
            "analiz": core.get("analiz"),
            "top_k": top_k,
            "alpha": alpha,
            "total_records": core.get("total_records", 0),
            "yeni_indexlenen": added,
            "corpus": corpus.stats(),
            "results": results,
        }


# --- Semantic Search Tool (Conditional - requires OPENROUTER_API_KEY) ---
if SEMANTIC_SEARCH_AVAILABLE:
    @app.tool(
        description="Use this when you need intelligent semantic search on Turkish legal decisions. Uses AI embeddings for relevance re-ranking.",
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True
        }
    )
    async def search_bedesten_semantic(
        initial_keyword: str = Field(..., description="""Bedesten API'den ilk sonuçları çekmek için anahtar kelime veya arama ifadesi.
Bu terim ile API'den 100 karar çekilir, sonra semantik sıralama yapılır.

ARAMA OPERATÖRLERİ:
• Basit arama: "muvazaa" (kelimeyi içeren kararlar)
• Tam eşleşme: "\"muris muvazaası\"" (tırnak içi aynen aranır)
• AND: "muvazaa AND tapu" (her iki terim zorunlu)
• OR: "ecrimisil OR kira" (en az biri yeterli)
• NOT: "muvazaa NOT miras" (muvazaa içeren ama miras içermeyen)
• Zorunlu: "+muvazaa tapu" (muvazaa zorunlu, tapu opsiyonel)
• Hariç: "muvazaa -miras" (muvazaa içeren, miras hariç)

ÖRNEKLER:
• "muvazaa" - geniş arama
• "\"muris muvazaası\"" - tam ifade
• "muvazaa AND tapu AND iptal" - tüm terimler zorunlu
• "ecrimisil OR haksız işgal" - alternatifli arama"""),
        query: str = Field(..., description="""Semantik benzerlik için DETAYLI arama sorgusu.
initial_keyword ile bulunan kararlar bu sorguya göre anlamsal olarak sıralanır.

ÖNEMLİ: Embedding modeli anlamlı cümleler bekler, anahtar kelimeler DEĞİL.
Aradığınız hukuki meseleyi CÜMLE olarak yazın.

DOĞRU KULLANIM:
• "Mirasçının muvazaalı satış işlemine karşı tapu iptali ve tescil davası açması"
• "Taşınmazın fiili kullanımı ve zilyetlik durumunun değerlendirilmesi"
• "İş sözleşmesinin feshinde kıdem tazminatı hesaplama yöntemi"

YANLIŞ KULLANIM:
• "muvazaa tapu iptal" (sadece kelimeler, cümle değil)
• "kıdem tazminat hesap" (bağlamsız kelimeler)

İPUCU: Ne arıyorsanız onu bir cümle olarak ifade edin."""),
        court_types: List[BedestenCourtTypeEnum] = Field(
            default=["YARGITAYKARARI", "DANISTAYKARAR", "YERELHUKUK", "ISTINAFHUKUK", "KYB"],
            description="Court types to search: YARGITAYKARARI, DANISTAYKARAR, YERELHUKUK, ISTINAFHUKUK, KYB (default: all)"
        ),
        top_k: int = Field(10, ge=1, le=50, description="Number of top results to return (1-50)"),
        belge_sayisi: int = Field(10, ge=0, le=25, description="How many NEW full texts to fetch and index into the persistent corpus (0 = index search metadata only). Documents already in the corpus are never re-fetched."),
        alpha: float = Field(0.6, ge=0.0, le=1.0, description="Weight of semantic similarity vs keyword relevance (0 = pure keyword, 1 = pure semantic).")
    ) -> Dict[str, Any]:
        """
        Perform semantic search on Turkish legal decisions using OpenRouter API.

        This tool:
        1. Searches Bedesten API with initial keyword (retrieves up to 100 results)
        2. Fetches full document content ONLY for documents not already in the
           persistent vector corpus (SEMANTIC_INDEX_DIR) - repeated searches reuse
           previously indexed texts, making them fast and cheap
        3. Generates embeddings using Google's Gemini Embedding model via OpenRouter
        4. Performs hybrid similarity search with the query (semantic + keyword)
        5. Returns re-ranked results based on relevance

        Benefits over keyword search:
        - Better understanding of context and meaning
        - Finds semantically similar documents even with different wording
        - More accurate ranking based on relevance
        - Supports multilingual queries (100+ languages)
        - Shares the persistent corpus with ictihat_semantik_ara

        Note: Requires OPENROUTER_API_KEY environment variable to be set.
        """
        logger.info("search_bedesten_semantic called with initial_keyword=%r query=%r", initial_keyword, query)

        try:
            embedder = get_embedder()
            corpus = SemanticCorpus(model=embedder.model, dimension=embedder.dimension)
            max_chars = int(os.getenv("SEMANTIC_MAX_CHARS", "2000"))

            # Step 1: Initial keyword search to get document IDs
            logger.info("Step 1: Searching Bedesten API with keyword: %r", initial_keyword)

            all_decisions: List[Dict[str, Any]] = []

            for court_type in court_types:
                try:
                    per_court_limit = max(20, 100 // len(court_types))

                    search_results = await bedesten_client_instance.search_documents(
                        BedestenSearchRequest(
                            data=BedestenSearchData(
                                phrase=initial_keyword,
                                itemTypeList=[court_type],
                                pageSize=per_court_limit,
                                pageNumber=1
                            )
                        )
                    )

                    if search_results.data and search_results.data.emsalKararList:
                        all_decisions.extend(d.model_dump() for d in search_results.data.emsalKararList)
                        logger.info("Found %d results from %s", len(search_results.data.emsalKararList), court_type)

                except Exception as e:
                    logger.warning("Error searching %s: %s", court_type, e)

            if not all_decisions:
                logger.warning("No documents found from initial search")
                return {
                    "status": "no_results",
                    "message": "No documents found matching the initial keyword",
                    "results": []
                }

            logger.info("Total documents found: %d", len(all_decisions))

            # Step 2: Index only the documents not yet in the persistent corpus
            logger.info("Step 2: Indexing new documents into persistent corpus...")
            added = await _index_new_decisions(corpus, embedder, all_decisions, belge_sayisi, max_chars)
            logger.info("Indexed %d new documents", added)

            # Step 3-4: Hybrid re-ranking of the corpus
            logger.info("Step 3: Performing hybrid semantic search...")
            query_embedding = embedder.encode_query(query, task="search result")
            ranked = _hybrid_rank(
                corpus, query_embedding,
                _keyword_position_scores(all_decisions), alpha, top_k,
            )

            # Step 5: Format results
            formatted_results = []
            for doc, vec, kw, combined in ranked:
                title_parts = []
                if doc.metadata.get("birimAdi"):
                    title_parts.append(doc.metadata["birimAdi"])
                if doc.metadata.get("esasNo"):
                    title_parts.append(f"Esas: {doc.metadata['esasNo']}")
                if doc.metadata.get("kararNo"):
                    title_parts.append(f"Karar: {doc.metadata['kararNo']}")
                if doc.metadata.get("kararTarihiStr") or doc.metadata.get("kararTarihi"):
                    title_parts.append(f"Tarih: {doc.metadata.get('kararTarihiStr') or doc.metadata.get('kararTarihi')}")

                title = " - ".join(title_parts) if title_parts else f"Document {doc.id}"
                text = doc.text or ""

                formatted_results.append({
                    "document_id": doc.id,
                    "title": title,
                    "similarity_score": round(float(vec), 4),
                    "keyword_score": round(float(kw), 4),
                    "combined_score": round(float(combined), 4),
                    "preview": text[:500] + "..." if len(text) > 500 else text,
                    "metadata": doc.metadata,
                    "source_url": f"https://mevzuat.adalet.gov.tr/ictihat/{doc.id}"
                })

            return {
                "status": "success",
                "query": query,
                "initial_keyword": initial_keyword,
                "top_k": top_k,
                "alpha": alpha,
                "total_documents_processed": len(all_decisions),
                "yeni_indexlenen": added,
                "embedding_model": embedder.model,
                "embedding_dimension": embedder.dimension,
                "results": formatted_results,
                "corpus": corpus.stats(),
                "stats": {
                    "documents_in_store": corpus.size,
                }
            }

        except Exception as e:
            logger.exception("Error in semantic search: %s", e)
            return {
                "status": "error",
                "message": str(e),
                "results": []
            }


# --- Benzer Karar Bulma (Kalıcı Korpus Üzerinden) ---
if SEMANTIC_SEARCH_AVAILABLE:
    @app.tool(
        description=(
            "Finds semantically similar decisions to a given documentId or a free-text "
            "legal issue, using the persistent vector corpus (shared with ictihat_semantik_ara). "
            "For precedent research: once a relevant decision is found, get the similar "
            "decisions around it."
        ),
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True
        }
    )
    async def ictihat_benzer_bul(
        belge_id: str = Field("", description="documentId of a decision already in the corpus (from ictihat_semantik_ara or search_bedesten_semantic results). Optional if metin is given. When both are given, metin is used as the query and belge_id is excluded from the results."),
        metin: str = Field("", description="Free-text description of the legal issue to compare against the corpus. Should be a full sentence (e.g. 'Mirasçının muvazaalı satış işlemine karşı tapu iptali ve tescil davası açması'), not keywords. Optional if belge_id is given."),
        top_k: int = Field(10, ge=1, le=50, description="Number of similar decisions to return (1-50)."),
        esik: float = Field(0.0, ge=0.0, le=1.0, description="Minimum semantic similarity threshold (0-1) to include a result. 0 = no filtering.")
    ) -> Dict[str, Any]:
        """Find semantically similar decisions in the persistent vector corpus."""
        logger.info("ictihat_benzer_bul called with belge_id=%r metin=%r", belge_id, metin)

        if not belge_id.strip() and not metin.strip():
            return {"error_message": "At least one of belge_id or metin is required.", "results": []}

        try:
            embedder = get_embedder()
        except Exception as e:
            logger.exception("ictihat_benzer_bul: embedder init failed")
            return {"error_message": f"Embedding provider unavailable: {e}", "results": []}

        corpus = SemanticCorpus(model=embedder.model, dimension=embedder.dimension)
        if corpus.size == 0:
            return {
                "message": "The vector corpus is empty. Run ictihat_semantik_ara or search_bedesten_semantic first to index decisions.",
                "corpus": corpus.stats(),
                "results": [],
            }

        exclude_ids = set()
        if belge_id.strip():
            source_doc = corpus.get(belge_id)
            if source_doc is None:
                return {
                    "error_message": f"belge_id '{belge_id}' is not in the corpus. Use ictihat_semantik_ara to index it first.",
                    "corpus": corpus.stats(),
                    "results": [],
                }
            exclude_ids.add(belge_id)
            query_text = metin.strip() or source_doc.text
            query_source = "metin" if metin.strip() else "belge_id"
        else:
            query_text = metin.strip()
            query_source = "metin"

        query_embedding = embedder.encode_query(query_text)
        vector_results = corpus.search(query_embedding, top_k=corpus.size, threshold=esik if esik > 0 else None)

        results = []
        for doc, score in vector_results:
            if doc.id in exclude_ids:
                continue
            text = doc.text or ""
            results.append({
                "documentId": doc.id,
                "court_type": doc.metadata.get("itemType", {}).get("name") if isinstance(doc.metadata.get("itemType"), dict) else doc.metadata.get("court_type"),
                "birim_adi": doc.metadata.get("birimAdi"),
                "esas_no": doc.metadata.get("esasNo"),
                "karar_no": doc.metadata.get("kararNo"),
                "karar_tarihi": doc.metadata.get("kararTarihiStr") or doc.metadata.get("kararTarihi"),
                "similarity_score": round(float(score), 4),
                "onizleme": text[:300] + "..." if len(text) > 300 else text,
            })
            if len(results) >= top_k:
                break

        return {
            "query_source": query_source,
            "query_orijinal": belge_id if query_source == "belge_id" else metin,
            "top_k": top_k,
            "esik": esik,
            "corpus": corpus.stats(),
            "results": results,
        }


# --- MCP Tools for Sayıştay (Turkish Court of Accounts) ---

# DEACTIVATED TOOL - Use search_sayistay_unified instead
# @app.tool(
#     description="Search Sayıştay Genel Kurul decisions for audit and accountability regulations",
#     annotations={
#         "readOnlyHint": True,
#         "openWorldHint": True,
#         "idempotentHint": True
#     }
# )
# async def search_sayistay_genel_kurul(
#     karar_no: str = Field("", description="Decision number to search for (e.g., '5415')"),
#     karar_ek: str = Field("", description="Decision appendix number (max 99, e.g., '1')"),
#     karar_tarih_baslangic: str = Field("", description="Start date (DD.MM.YYYY)"),
#     karar_tarih_bitis: str = Field("", description="End date (DD.MM.YYYY)"),
#     karar_tamami: str = Field("", description="Full text search"),
#     start: int = Field(0, description="Starting record for pagination (0-based)"),
#     length: int = Field(10, description="Number of records per page (1-100)")
# ) -> GenelKurulSearchResponse:
#     """Search Sayıştay General Assembly decisions."""
#     raise ValueError("This tool is deactivated. Use search_sayistay_unified instead.")

# DEACTIVATED TOOL - Use search_sayistay_unified instead
# @app.tool(
#     description="Search Sayıştay Temyiz Kurulu decisions with chamber filtering and comprehensive criteria",
#     annotations={
#         "readOnlyHint": True,
#         "openWorldHint": True,
#         "idempotentHint": True
#     }
# )
# async def search_sayistay_temyiz_kurulu(
#     ilam_dairesi: DaireEnum = Field("ALL", description="Audit chamber selection"),
#     yili: str = Field("", description="Year (YYYY)"),
#     karar_tarih_baslangic: str = Field("", description="Start date (DD.MM.YYYY)"),
#     karar_tarih_bitis: str = Field("", description="End date (DD.MM.YYYY)"),
#     kamu_idaresi_turu: KamuIdaresiTuruEnum = Field("ALL", description="Public admin type"),
#     ilam_no: str = Field("", description="Audit report number (İlam No, max 50 chars)"),
#     dosya_no: str = Field("", description="File number for the case"),
#     temyiz_tutanak_no: str = Field("", description="Appeals board meeting minutes number"),
#     temyiz_karar: str = Field("", description="Appeals decision text"),
#     web_karar_konusu: WebKararKonusuEnum = Field("ALL", description="Decision subject"),
#     start: int = Field(0, description="Starting record for pagination (0-based)"),
#     length: int = Field(10, description="Number of records per page (1-100)")
# ) -> TemyizKuruluSearchResponse:
#     """Search Sayıştay Appeals Board decisions."""
#     raise ValueError("This tool is deactivated. Use search_sayistay_unified instead.")

# DEACTIVATED TOOL - Use search_sayistay_unified instead
# @app.tool(
#     description="Search Sayıştay Daire decisions with chamber filtering and subject categorization",
#     annotations={
#         "readOnlyHint": True,
#         "openWorldHint": True,
#         "idempotentHint": True
#     }
# )
# async def search_sayistay_daire(
#     yargilama_dairesi: DaireEnum = Field("ALL", description="Chamber selection"),
#     karar_tarih_baslangic: str = Field("", description="Start date (DD.MM.YYYY)"),
#     karar_tarih_bitis: str = Field("", description="End date (DD.MM.YYYY)"),
#     ilam_no: str = Field("", description="Audit report number (İlam No, max 50 chars)"),
#     kamu_idaresi_turu: KamuIdaresiTuruEnum = Field("ALL", description="Public admin type"),
#     hesap_yili: str = Field("", description="Fiscal year"),
#     web_karar_konusu: WebKararKonusuEnum = Field("ALL", description="Decision subject"),
#     web_karar_metni: str = Field("", description="Decision text search"),
#     start: int = Field(0, description="Starting record for pagination (0-based)"),
#     length: int = Field(10, description="Number of records per page (1-100)")
# ) -> DaireSearchResponse:
#     """Search Sayıştay Chamber decisions."""
#     raise ValueError("This tool is deactivated. Use search_sayistay_unified instead.")

# DEACTIVATED TOOL - Use get_sayistay_document_unified instead
# @app.tool(
#     description="Get Sayıştay Genel Kurul decision document in Markdown format",
#     annotations={
#         "readOnlyHint": True,
#         "openWorldHint": False,
#         "idempotentHint": True
#     }
# )
# async def get_sayistay_genel_kurul_document_markdown(
#     decision_id: str = Field(..., description="Decision ID from search_sayistay_genel_kurul results")
# ) -> SayistayDocumentMarkdown:
#     """Get Sayıştay General Assembly decision as Markdown."""
#     raise ValueError("This tool is deactivated. Use get_sayistay_document_unified instead.")

# DEACTIVATED TOOL - Use get_sayistay_document_unified instead
# @app.tool(
#     description="Get Sayıştay Temyiz Kurulu decision document in Markdown format",
#     annotations={
#         "readOnlyHint": True,
#         "openWorldHint": False,
#         "idempotentHint": True
#     }
# )
# async def get_sayistay_temyiz_kurulu_document_markdown(
#     decision_id: str = Field(..., description="Decision ID from search_sayistay_temyiz_kurulu results")
# ) -> SayistayDocumentMarkdown:
#     """Get Sayıştay Appeals Board decision as Markdown."""
#     raise ValueError("This tool is deactivated. Use get_sayistay_document_unified instead.")

# DEACTIVATED TOOL - Use get_sayistay_document_unified instead
# @app.tool(
#     description="Get Sayıştay Daire decision document in Markdown format",
#     annotations={
#         "readOnlyHint": True,
#         "openWorldHint": False,
#         "idempotentHint": True
#     }
# )
# async def get_sayistay_daire_document_markdown(
#     decision_id: str = Field(..., description="Decision ID from search_sayistay_daire results")
# ) -> SayistayDocumentMarkdown:
#     """Get Sayıştay Chamber decision as Markdown."""
#     raise ValueError("This tool is deactivated. Use get_sayistay_document_unified instead.")

# --- UNIFIED MCP Tools for Sayıştay (Turkish Court of Accounts) ---

@app.tool(
    description="Use this when searching Turkish Court of Accounts (Sayıştay) audit decisions. Supports Genel Kurul, Temyiz Kurulu, and Daire decisions.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_sayistay_unified(
    decision_type: Literal["genel_kurul", "temyiz_kurulu", "daire"] = Field(..., description="Decision type: genel_kurul, temyiz_kurulu, or daire"),
    
    # Common pagination parameters
    start: int = Field(0, ge=0, description="Starting record for pagination (0-based)"),
    length: int = Field(10, ge=1, le=100, description="Number of records per page (1-100)"),
    
    # Common search parameters
    karar_tarih_baslangic: str = Field("", description="Start date (DD.MM.YYYY format)"),
    karar_tarih_bitis: str = Field("", description="End date (DD.MM.YYYY format)"),
    kamu_idaresi_turu: Literal["ALL", "Genel Bütçe Kapsamındaki İdareler", "Yüksek Öğretim Kurumları", "Diğer Özel Bütçeli İdareler", "Düzenleyici ve Denetleyici Kurumlar", "Sosyal Güvenlik Kurumları", "Özel İdareler", "Belediyeler ve Bağlı İdareler", "Diğer"] = Field("ALL", description="Public administration type filter"),
    ilam_no: str = Field("", description="Audit report number (İlam No, max 50 chars)"),
    web_karar_konusu: Literal["ALL", "Harcırah Mevzuatı", "İhale Mevzuatı", "İş Mevzuatı", "Personel Mevzuatı", "Sorumluluk ve Yargılama Usulleri", "Vergi Resmi Harç ve Diğer Gelirler", "Çeşitli Konular"] = Field("ALL", description="Decision subject category filter"),
    
    # Genel Kurul specific parameters (ignored for other types)
    karar_no: str = Field("", description="Decision number (genel_kurul only)"),
    karar_ek: str = Field("", description="Decision appendix number (genel_kurul only)"),
    karar_tamami: str = Field("", description="Full text search (genel_kurul only)"),
    
    # Temyiz Kurulu specific parameters (ignored for other types)
    ilam_dairesi: Literal["ALL", "1", "2", "3", "4", "5", "6", "7", "8"] = Field("ALL", description="Audit chamber selection (temyiz_kurulu only)"),
    yili: str = Field("", description="Year (YYYY format, temyiz_kurulu only)"),
    dosya_no: str = Field("", description="File number (temyiz_kurulu only)"),
    temyiz_tutanak_no: str = Field("", description="Appeals board meeting minutes number (temyiz_kurulu only)"),
    temyiz_karar: str = Field("", description="Appeals decision text search (temyiz_kurulu only)"),
    
    # Daire specific parameters (ignored for other types)
    yargilama_dairesi: Literal["ALL", "1", "2", "3", "4", "5", "6", "7", "8"] = Field("ALL", description="Chamber selection (daire only)"),
    hesap_yili: str = Field("", description="Account year (daire only)"),
    web_karar_metni: str = Field("", description="Decision text search (daire only)")
) -> Dict[str, Any]:
    """Search Sayıştay decisions across all three decision types with unified interface."""
    logger.info(f"Tool 'search_sayistay_unified' called with decision_type={decision_type}")

    try:
        search_request = SayistayUnifiedSearchRequest(
            decision_type=decision_type,
            start=start,
            length=length,
            karar_tarih_baslangic=karar_tarih_baslangic,
            karar_tarih_bitis=karar_tarih_bitis,
            kamu_idaresi_turu=kamu_idaresi_turu,
            ilam_no=ilam_no,
            web_karar_konusu=web_karar_konusu,
            karar_no=karar_no,
            karar_ek=karar_ek,
            karar_tamami=karar_tamami,
            ilam_dairesi=ilam_dairesi,
            yili=yili,
            dosya_no=dosya_no,
            temyiz_tutanak_no=temyiz_tutanak_no,
            temyiz_karar=temyiz_karar,
            yargilama_dairesi=yargilama_dairesi,
            hesap_yili=hesap_yili,
            web_karar_metni=web_karar_metni
        )
        result = await sayistay_unified_client_instance.search_unified(search_request)
        return result.model_dump()
    except Exception:
        logger.exception("Error in tool 'search_sayistay_unified'")
        raise

@app.tool(
    description="Use this when retrieving full text of a Sayıştay audit decision. Returns clean Markdown format.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_sayistay_document_unified(
    decision_id: str = Field(..., description="Decision ID from search_sayistay_unified results"),
    decision_type: Literal["genel_kurul", "temyiz_kurulu", "daire"] = Field(..., description="Decision type: genel_kurul, temyiz_kurulu, or daire")
) -> Dict[str, Any]:
    """Get Sayıştay decision document as Markdown for any decision type."""
    logger.info(f"Tool 'get_sayistay_document_unified' called for ID: {decision_id}, type: {decision_type}")

    if not decision_id or not decision_id.strip():
        raise ValueError("Decision ID must be a non-empty string.")

    try:
        result = await sayistay_unified_client_instance.get_document_unified(decision_id, decision_type)
        return result.model_dump()
    except Exception:
        logger.exception("Error in tool 'get_sayistay_document_unified'")
        raise

# --- Application Shutdown Handling ---
def perform_cleanup():
    logger.info("MCP Server performing cleanup...")
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if loop.is_closed(): 
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError: 
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    clients_to_close = [
        globals().get('yargitay_client_instance'),
        globals().get('danistay_client_instance'),
        globals().get('emsal_client_instance'),
        globals().get('uyusmazlik_client_instance'),
        globals().get('anayasa_norm_client_instance'),
        globals().get('anayasa_bireysel_client_instance'),
        globals().get('anayasa_unified_client_instance'),
        globals().get('kik_v2_client_instance'),
        globals().get('rekabet_client_instance'),
        globals().get('bedesten_client_instance'),
        globals().get('sayistay_client_instance'),
        globals().get('sayistay_unified_client_instance'),
        globals().get('kvkk_client_instance'),
        globals().get('bddk_client_instance'),
        globals().get('btk_client_instance'),
        globals().get('gib_client_instance'),
        globals().get('sigorta_tahkim_client_instance'),
        globals().get('mevzuat_client_instance'),
        globals().get('resmi_gazete_client_instance'),
        globals().get('aihm_client_instance')
    ]
    async def close_all_clients_async():
        tasks = []
        for client_instance in clients_to_close:
            if client_instance and hasattr(client_instance, 'close_client_session') and callable(client_instance.close_client_session):
                logger.info(f"Scheduling close for client session: {client_instance.__class__.__name__}")
                tasks.append(client_instance.close_client_session())
        # Close health check client if it was created
        global _health_check_client
        if _health_check_client is not None:
            logger.info("Closing health check HTTP client")
            tasks.append(_health_check_client.aclose())
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    client_name = "Unknown Client"
                    if i < len(clients_to_close) and clients_to_close[i] is not None:
                        client_name = clients_to_close[i].__class__.__name__
                    logger.error(f"Error closing client {client_name}: {result}")
    try:
        if loop.is_running(): 
            asyncio.ensure_future(close_all_clients_async(), loop=loop)
            logger.info("Client cleanup tasks scheduled on running event loop.")
        else:
            loop.run_until_complete(close_all_clients_async())
            logger.info("Client cleanup tasks completed via run_until_complete.")
    except Exception as e: 
        logger.error(f"Error during atexit cleanup execution: {e}", exc_info=True)
    logger.info("MCP Server atexit cleanup process finished.")

atexit.register(perform_cleanup)


def get_or_create_health_check_client() -> httpx.AsyncClient:
    """Get or create a reusable HTTP client for health checks."""
    global _health_check_client
    if _health_check_client is None:
        _health_check_client = httpx.AsyncClient(
            timeout=10.0,
            verify=False,
            follow_redirects=True
        )
    return _health_check_client


# --- Health Check Tools ---
@app.tool(
    description="Use this when checking if Turkish legal database servers are online and responding.",
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
async def check_government_servers_health() -> Dict[str, Any]:
    """Check health status of Turkish government legal database servers."""
    logger.info("Health check tool called for government servers")
    
    health_results = {}
    
    # Check Yargıtay server
    try:
        yargitay_payload = {
            "data": {
                "aranan": "karar",
                "arananKelime": "karar", 
                "pageSize": 10,
                "pageNumber": 1
            }
        }
        
        async with httpx.AsyncClient(
            headers={
                "Accept": "*/*",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Connection": "keep-alive",
                "Content-Type": "application/json; charset=UTF-8",
                "Origin": "https://karararama.yargitay.gov.tr",
                "Referer": "https://karararama.yargitay.gov.tr/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors", 
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "X-Requested-With": "XMLHttpRequest"
            },
            timeout=30.0,
            verify=False
        ) as client:
            response = await client.post(
                "https://karararama.yargitay.gov.tr/aramalist",
                json=yargitay_payload
            )
        
        if response.status_code == 200:
            response_data = response.json()
            records_total = response_data.get("data", {}).get("recordsTotal", 0)
            
            if records_total > 0:
                health_results["yargitay"] = {
                    "status": "healthy",
                    "response_time_ms": response.elapsed.total_seconds() * 1000
                }
            else:
                health_results["yargitay"] = {
                    "status": "unhealthy", 
                    "reason": "recordsTotal is 0 or missing",
                    "response_time_ms": response.elapsed.total_seconds() * 1000
                }
        else:
            health_results["yargitay"] = {
                "status": "unhealthy", 
                "reason": f"HTTP {response.status_code}",
                "response_time_ms": response.elapsed.total_seconds() * 1000
            }
        
    except Exception as e:
        health_results["yargitay"] = {
            "status": "unhealthy",
            "reason": f"Connection error: {str(e)}"
        }
    
    # Check Bedesten API server
    try:
        bedesten_payload = {
            "data": {
                "pageSize": 5,
                "pageNumber": 1,
                "itemTypeList": ["YARGITAYKARARI"], 
                "phrase": "karar",
                "sortFields": ["KARAR_TARIHI"],
                "sortDirection": "desc"
            },
            "applicationName": "UyapMevzuat",
            "paging": True
        }
        
        client = get_or_create_health_check_client()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 Health Check"
        }
        
        response = await client.post(
            "https://bedesten.adalet.gov.tr/emsal-karar/searchDocuments",
            json=bedesten_payload,
            headers=headers
        )
        
        if response.status_code == 200:
            response_data = response.json()
            logger.debug(f"Bedesten API response: {response_data}")
            if response_data and isinstance(response_data, dict):
                data_section = response_data.get("data")
                if data_section and isinstance(data_section, dict):
                    total_found = data_section.get("total", 0)
                else:
                    total_found = 0
            else:
                total_found = 0
            
            if total_found > 0:
                health_results["bedesten"] = {
                    "status": "healthy", 
                    "response_time_ms": response.elapsed.total_seconds() * 1000
                }
            else:
                health_results["bedesten"] = {
                    "status": "unhealthy",
                    "reason": "total is 0 or missing in data field",
                    "response_time_ms": response.elapsed.total_seconds() * 1000
                }
        else:
            health_results["bedesten"] = {
                "status": "unhealthy",
                "reason": f"HTTP {response.status_code}",
                "response_time_ms": response.elapsed.total_seconds() * 1000
            }
        
    except Exception as e:
        health_results["bedesten"] = {
            "status": "unhealthy", 
            "reason": f"Connection error: {str(e)}"
        }
    
    # Overall health assessment
    healthy_servers = sum(1 for server in health_results.values() if server["status"] == "healthy")
    total_servers = len(health_results)
    
    overall_status = "healthy" if healthy_servers == total_servers else "degraded" if healthy_servers > 0 else "unhealthy"
    
    return {
        "overall_status": overall_status,
        "healthy_servers": healthy_servers,
        "total_servers": total_servers,
        "servers": health_results,
        "check_timestamp": f"{__import__('datetime').datetime.now().isoformat()}"
    }

# --- MCP Tools for KVKK ---
@app.tool(
    description="Use this when searching Turkish data protection (KVKK/GDPR equivalent) decisions. For privacy, consent, and data breach cases.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_kvkk_decisions(
    keywords: str = Field(..., description="Turkish keywords. Supports +required -excluded \"exact phrase\" operators"),
    page: int = Field(1, ge=1, le=50, description="Page number for results (1-50)."),
    # pageSize: int = Field(10, ge=1, le=20, description="Number of results per page (1-20).")
) -> Dict[str, Any]:
    """Search function for legal decisions."""
    logger.info(f"KVKK search tool called with keywords: {keywords}")

    pageSize = 10  # Default value

    search_request = KvkkSearchRequest(
        keywords=keywords,
        page=page,
        pageSize=pageSize
    )

    try:
        result = await kvkk_client_instance.search_decisions(search_request)
        logger.info(f"KVKK search completed. Found {len(result.decisions)} decisions on page {page}")
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error in KVKK search: {e}")
        # Return empty result on error
        return KvkkSearchResult(
            decisions=[],
            total_results=0,
            page=page,
            pageSize=pageSize,
            query=keywords
        ).model_dump()

@app.tool(
    description="Use this when retrieving full text of a KVKK data protection decision. Returns paginated Markdown with metadata.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_kvkk_document_markdown(
    decision_url: str = Field(..., description="KVKK decision URL from search results"),
    page_number: int = Field(1, ge=1, description="Page number for paginated Markdown content (1-indexed, accepts int). Default is 1 (first 5,000 characters).")
) -> Dict[str, Any]:
    """Get KVKK decision as paginated Markdown."""
    logger.info(f"KVKK document retrieval tool called for URL: {decision_url}")

    if not decision_url or not decision_url.strip():
        return KvkkDocumentMarkdown(
            source_url=HttpUrl("https://www.kvkk.gov.tr"),
            title=None,
            decision_date=None,
            decision_number=None,
            subject_summary=None,
            markdown_chunk=None,
            current_page=page_number or 1,
            total_pages=0,
            is_paginated=False,
            error_message="Decision URL is required and cannot be empty."
        ).model_dump()
    
    try:
        # Validate URL format
        if not decision_url.startswith("https://www.kvkk.gov.tr/"):
            return KvkkDocumentMarkdown(
                source_url=HttpUrl(decision_url),
                title=None,
                decision_date=None,
                decision_number=None,
                subject_summary=None,
                markdown_chunk=None,
                current_page=page_number or 1,
                total_pages=0,
                is_paginated=False,
                error_message="Invalid KVKK decision URL format. URL must start with https://www.kvkk.gov.tr/"
            ).model_dump()

        result = await kvkk_client_instance.get_decision_document(decision_url, page_number or 1)
        logger.info(f"KVKK document retrieved successfully. Page {result.current_page}/{result.total_pages}, Content length: {len(result.markdown_chunk) if result.markdown_chunk else 0}")
        return result.model_dump()
        
    except Exception as e:
        logger.exception(f"Error retrieving KVKK document: {e}")
        return KvkkDocumentMarkdown(
            source_url=HttpUrl(decision_url),
            title=None,
            decision_date=None,
            decision_number=None,
            subject_summary=None,
            markdown_chunk=None,
            current_page=page_number or 1,
            total_pages=0,
            is_paginated=False,
            error_message=f"Error retrieving KVKK document: {str(e)}"
        ).model_dump()

# --- MCP Tools for KDK (Kamu Denetçiliği Kurumu / Ombudsmanlık) ---
@app.tool(
    description="Use this when searching Turkish Ombudsman (Kamu Denetçiliği Kurumu / KDK) decisions (Tavsiye/Ret kararları). Returns decision summaries with evrak_id for full-text retrieval.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_kdk_decisions(
    keywords: Optional[str] = Field("", description="Turkish keywords searched in the decision subject (evrak konusu). Examples: 'eğitim hakkı', 'tapu iptali', 'öğretmen ataması'"),
    karar_turu: Optional[str] = Field(None, description="Decision type filter. Values: 'Tavsiye Kararı', 'Ret Kararı', 'Kısmen Tavsiye Kısmen Ret Kararı' etc."),
    sikayet_konu: Optional[str] = Field(None, description="Complaint subject category filter. Values e.g. 'Eğitim-öğretim, gençlik ve spor', 'Sağlık', 'Mülkiyet hakkı', 'Kamu personel rejimi'"),
    page: int = Field(1, ge=1, le=100, description="Page number for results (1-100)."),
    page_size: int = Field(10, ge=1, le=50, description="Results per page (1-50).")
) -> Dict[str, Any]:
    """Search KDK (Ombudsman) decisions."""
    logger.info(f"KDK search tool called with keywords: {keywords}, karar_turu: {karar_turu}")

    search_request = KdkSearchRequest(
        keywords=keywords or "",
        karar_turu=karar_turu,
        sikayet_konu=sikayet_konu,
        page=page,
        pageSize=page_size
    )

    try:
        result = await kdk_client_instance.search_decisions(search_request)
        logger.info(f"KDK search completed. Found {len(result.decisions)} decisions on page {page}, total={result.total_results}")
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error in KDK search: {e}")
        return KdkSearchResult(
            decisions=[],
            total_results=0,
            page=page,
            pageSize=page_size,
            query=keywords or ""
        ).model_dump()

@app.tool(
    description="Use this when retrieving full text of a Turkish Ombudsman (KDK) decision. Returns paginated Markdown. Optionally pass the PDF info from search results (yayin_url, karar_tarihi) to enable PDF fallback when OCR is not ready.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_kdk_document_markdown(
    evrak_id: int = Field(..., description="evrak_id (document id) from search_kdk_decisions results"),
    page_number: int = Field(1, ge=1, description="Page number for paginated Markdown content (1-indexed). Default is 1 (first 5,000 characters)."),
    yayin_url: Optional[str] = Field(None, description="Optional published PDF path (karaR_YAYIN_URL) from search results, used as fallback when OCR text is not ready."),
    karar_tarihi: Optional[str] = Field(None, description="Optional decision date (karaR_TARIH) from search results, needed for the PDF download fallback.")
) -> Dict[str, Any]:
    """Get KDK decision as paginated Markdown."""
    logger.info(f"KDK document retrieval tool called for evrak_id: {evrak_id}")

    try:
        result = await kdk_client_instance.get_decision_document(
            evrak_id, page_number or 1,
            pdf_url=yayin_url, tarih=karar_tarihi
        )
        logger.info(f"KDK document retrieved. Page {result.current_page}/{result.total_pages}")
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error retrieving KDK document: {e}")
        return KdkDocumentMarkdown(
            source_id=f"kdk:{evrak_id}",
            source_url=None,
            karar_no=None,
            basvuru_no=None,
            karar_turu=None,
            idare=None,
            konu=None,
            markdown_chunk=None,
            current_page=page_number or 1,
            total_pages=0,
            is_paginated=False,
            error_message=f"Error retrieving KDK document: {str(e)}"
        ).model_dump()

# --- MCP Tools for SPK (Sermaye Piyasası Kurulu) ---
@app.tool(
    description="Use this when searching Turkish Capital Markets Board (SPK) documents: ilke kararları (Kurul Kararları), rehberler, tebliğler, yönetmelikler. Filter by document type and related bülten.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_spk_decisions(
    keywords: Optional[str] = Field("", description="Turkish keywords searched in SPK documents (title and/or content). Examples: 'açığa satış', 'türev araç', 'halka arz'"),
    search_field: Optional[str] = Field("all", description="Search field: 'all' (both), 'title' (title only), 'content' (content only)."),
    tur: Optional[str] = Field(None, description="Document type filter. Values: 'Kurul Kararı' (ilke kararları), 'Rehber', 'Tebliğ', 'Yönetmelik', 'Kanun', 'Diğer Karar', 'Diğer'."),
    bulten_yili: Optional[int] = Field(None, description="Related weekly bulletin year filter (e.g. 2026)."),
    bulten_no: Optional[int] = Field(None, description="Related weekly bulletin number filter (e.g. 38)."),
    page: int = Field(1, ge=1, le=100, description="Page number for results (1-100)."),
    page_size: int = Field(10, ge=1, le=50, description="Results per page (1-50).")
) -> Dict[str, Any]:
    """Search SPK documents."""
    logger.info(f"SPK search tool called with keywords: {keywords}, tur: {tur}")

    search_request = SpkSearchRequest(
        keywords=keywords or "",
        search_field=search_field,
        tur=tur,
        bulten_yili=bulten_yili,
        bulten_no=bulten_no,
        page=page,
        pageSize=page_size
    )

    try:
        result = await spk_client_instance.search_decisions(search_request)
        logger.info(f"SPK search completed. Found {len(result.decisions)} documents on page {page}, total={result.total_results}")
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error in SPK search: {e}")
        return SpkSearchResult(
            decisions=[],
            total_results=0,
            page=page,
            pageSize=page_size,
            query=keywords or ""
        ).model_dump()

@app.tool(
    description="Use this when retrieving full text of an SPK document (ilke kararı / rehber / tebliğ / yönetmelik / haftalık bülten) as paginated Markdown. Accepts document_id from search_spk_decisions results (e.g. 'spk:ilke:369', 'spk:rehber:12', 'spk:bulten:2026/38').",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_spk_document_markdown(
    document_id: str = Field(..., description="SPK document id from search_spk_decisions results, format 'spk:<ilke|rehber|mevzuat|bulten>:<id>' (e.g. 'spk:ilke:369', 'spk:bulten:2026/38')."),
    page_number: int = Field(1, ge=1, description="Page number for paginated Markdown content (1-indexed). Default is 1 (first 5,000 characters).")
) -> Dict[str, Any]:
    """Get SPK document as paginated Markdown."""
    logger.info(f"SPK document retrieval tool called for document_id: {document_id}")

    if not document_id or not document_id.strip():
        return SpkDocumentMarkdown(
            source_id=document_id,
            source_url=None,
            baslik=None,
            tur=None,
            tarih=None,
            toplanti_no=None,
            markdown_chunk=None,
            current_page=page_number or 1,
            total_pages=0,
            is_paginated=False,
            error_message="Document ID is required and cannot be empty."
        ).model_dump()

    try:
        result = await spk_client_instance.get_document(document_id.strip(), page_number or 1)
        logger.info(f"SPK document retrieved. Page {result.current_page}/{result.total_pages}")
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error retrieving SPK document: {e}")
        return SpkDocumentMarkdown(
            source_id=document_id,
            source_url=None,
            baslik=None,
            tur=None,
            tarih=None,
            toplanti_no=None,
            markdown_chunk=None,
            current_page=page_number or 1,
            total_pages=0,
            is_paginated=False,
            error_message=f"Error retrieving SPK document: {str(e)}"
        ).model_dump()

@app.tool(
    description="Use this when listing SPK weekly bulletins (haftalık SPK bültenleri) for a year. Returns bulletin summaries with pdf_url and document_id (spk:bulten:<yil>/<no>) usable with get_spk_document_markdown.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def list_spk_bultenleri(
    yil: int = Field(..., ge=2005, le=2030, description="Bulletin year (e.g. 2026).")
) -> Dict[str, Any]:
    """List SPK weekly bulletins for a year."""
    logger.info(f"SPK bülten list tool called for year: {yil}")
    try:
        bultenler = await spk_client_instance.list_bultenler(yil)
        return {"yil": yil, "total": len(bultenler), "bultenler": [b.model_dump() for b in bultenler]}
    except Exception as e:
        logger.exception(f"Error listing SPK bültenler: {e}")
        return {"yil": yil, "total": 0, "bultenler": []}

# --- MCP Tools for TBB (Türkiye Barolar Birliği Disiplin Kurulu) ---
@app.tool(
    description="Use this when searching Turkish Bar Association (TBB) Disiplin Kurulu decisions. Avukat disiplin hukuku, 2005-present. Keywords are matched as a phrase by the source; add a year (yil) filter for speed. document_id (tbb:<id>) is usable with get_tbb_document_markdown. NOTE: the source site's own search breaks on queries containing ç/ö/ü/â/î/û (returns nothing); this tool works around it by ASCII-folding the query and filtering locally, so such queries still find properly-spelled matches. Single-word queries made entirely of those letters may still return 0.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_tbb_decisions(
    keywords: str = Field("", description="""
        Turkish keywords searched in the decision gerekçe/concept (phrase matching by the source).
        Examples: "meslekten çıkarma", "haksız rekabet", "2023/919"
    """),
    yil: Optional[int] = Field(None, ge=2005, le=2030, description="Decision year filter (2005-present), e.g. 2023."),
    page: int = Field(1, ge=1, le=100, description="Result page (1-100; site returns 10 per page)."),
    pageSize: int = Field(10, ge=1, le=10, description="Results per page (site fixed at 10; display only).")
) -> Dict[str, Any]:
    """Search TBB Disiplin Kurulu decisions."""
    logger.info(f"TBB search tool called with keywords: {keywords}, yil: {yil}, page: {page}")
    search_request = TbbSearchRequest(keywords=keywords, yil=yil, page=page, pageSize=pageSize)
    result = await tbb_client_instance.search_decisions(search_request)
    return result.model_dump()

@app.tool(
    description="Use this when retrieving full text of a TBB (Türkiye Barolar Birliği) Disiplin Kurulu decision. Returns paginated Markdown with Tarih/Esas/Karar metadata. Takes the detay_id (or tbb:<id> document_id) from search_tbb_decisions.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def get_tbb_document_markdown(
    detay_id: int = Field(..., description="TBB decision detay_id from search_tbb_decisions results (e.g. 1698)."),
    page_number: int = Field(1, ge=1, description="Page number for paginated Markdown content (1-indexed). Default 1.")
) -> Dict[str, Any]:
    """Retrieve full text of a TBB Disiplin Kurulu decision."""
    logger.info(f"TBB document tool called with detay_id: {detay_id}, page: {page_number}")
    try:
        document = await tbb_client_instance.get_document(detay_id=detay_id, page_number=page_number)
        return document.model_dump()
    except Exception as e:
        logger.exception(f"Error retrieving TBB document: {e}")
        return TbbDocumentMarkdown(
            source_id=f"tbb:{detay_id}",
            source_url=f"https://www.barobirlik.org.tr/DisiplinKararlariDetay/{detay_id}",
            tarih=None, esas_no=None, karar_no=None, ozet=None,
            markdown_chunk=None, current_page=page_number,
            total_pages=0, is_paginated=False,
            error_message=f"Error retrieving TBB document: {str(e)}"
        ).model_dump()

# --- MCP Tools for EPDK (Enerji Piyasası Düzenleme Kurumu) ---
@app.tool(
    description="Use this when searching EPDK (Enerji Piyasası Düzenleme Kurumu) Kurul decisions. Karar başlığı+açıklamasında aranır (tüm kelimeler aynı kayıtta). Piyasa (sektör) seçilebilir: elektrik (default), dogalgaz, petrol, lpg, denetim, enerji_donusumu. İlk arama piyasa listesini indirir (~150 istek, 1-2 dk); sonrakiler önbellekten gelir (12 saat). document_id (epdk:<content_id>) is usable with get_epdk_document_markdown.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_epdk_decisions(
    keywords: str = Field("", description="""
        Turkish keywords searched in the decision title/description (all words must appear in one record).
        Examples: "TORETOSAF", "bağlantı bedeli", "tarife"
    """),
    piyasa: Annotated[Optional[str], Field(description="""
        Piyasa (sektör) filter. Values: "elektrik" (default), "dogalgaz", "petrol", "lpg",
        "denetim", "enerji_donusumu".
    """)] = None,
    page: int = Field(1, ge=1, le=100, description="Result page (1-100)."),
    pageSize: int = Field(10, ge=1, le=50, description="Results per page (1-50).")
) -> Dict[str, Any]:
    """Search EPDK Kurul decisions."""
    logger.info(f"EPDK search tool called with keywords: {keywords}, piyasa: {piyasa}, page: {page}")
    try:
        search_request = EpdkSearchRequest(keywords=keywords, piyasa=piyasa, page=page, pageSize=pageSize)
        result = await epdk_client_instance.search_decisions(search_request)
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error searching EPDK decisions: {e}")
        return EpdkSearchResult(
            decisions=[], total_results=0, page=page, pageSize=pageSize,
            query=keywords, piyasa=piyasa
        ).model_dump()

@app.tool(
    description="Use this when retrieving full text of an EPDK (Enerji Piyasası Düzenleme Kurumu) Kurul kararı belgesi (docx/pdf). Returns paginated Markdown. Takes the content_id (or epdk:<content_id> document_id) from search_epdk_decisions.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def get_epdk_document_markdown(
    content_id: str = Field(..., description="EPDK content_id from search_epdk_decisions results (e.g. 12223)."),
    baslik: Annotated[Optional[str], Field(description="Karar başlığı (opsiyonel, görüntüleme için).")] = None,
    karar_no: Annotated[Optional[str], Field(description="Karar numarası (opsiyonel, görüntüleme için).")] = None,
    karar_tarihi: Annotated[Optional[str], Field(description="Karar tarihi (opsiyonel, görüntüleme için).")] = None,
    page_number: int = Field(1, ge=1, description="Page number for paginated Markdown content (1-indexed). Default 1.")
) -> Dict[str, Any]:
    """Retrieve full text of an EPDK Kurul kararı belgesi."""
    logger.info(f"EPDK document tool called with content_id: {content_id}, page: {page_number}")
    try:
        document = await epdk_client_instance.get_document(
            content_id=content_id, baslik=baslik, karar_no=karar_no,
            karar_tarihi=karar_tarihi, page_number=page_number
        )
        return document.model_dump()
    except Exception as e:
        logger.exception(f"Error retrieving EPDK document: {e}")
        return EpdkDocumentMarkdown(
            source_id=f"epdk:{content_id}",
            source_url=f"https://epdk.gov.tr/Detay/DownloadDocument?id={content_id}",
            baslik=baslik, karar_no=karar_no, karar_tarihi=karar_tarihi,
            belge_turu=None, markdown_chunk=None, current_page=page_number,
            total_pages=0, is_paginated=False,
            error_message=f"Error retrieving EPDK document: {str(e)}"
        ).model_dump()

# --- MCP Tools for HSK (Hâkimler ve Savcılar Kurulu İkinci Daire) ---
@app.tool(
    description="Use this when searching HSK (Hâkimler ve Savcılar Kurulu) İkinci Daire disiplin kararları. Hâkim/savcı disiplin hukuku. KORPUS TAMAMEN ANONİMDİR: esas/karar no ve tarih kaynakta '.....' olarak silinmiştir, yıl filtresi yoktur. Arama karar tam metninde diakritik duyarsız AND aramasıdır (kelimeler yan yana olmak zorunda değil). İlk arama ~40 PDF indirir ve çevirir (1-2 dk); sonrakiler önbellekten gelir. document_id (hsk:disiplin:<uuid>:<sira>) is usable with get_hsk_document_markdown.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_hsk_decisions(
    keywords: str = Field("", description="""
        Turkish keywords searched in the full decision text (all must appear in one decision, diacritic-insensitive).
        Examples: "rüşvet", "mesleğin onurunu zedeleyen", "özel hayat"
    """),
    madde: Annotated[Optional[str], Field(description="""
        Yaptırım maddesi filter. Values: "uyarma", "ayliktan_kesme", "kinama",
        "kademe_ilerlemesi_durdurma", "derece_yukselmesi_durdurma", "yer_degistirme",
        "meslekten_cikarma", "ceza_tayinine_yer_olmadigi", "islemden_kaldirma".
        Empty = all maddeler.
    """)] = None,
    page: int = Field(1, ge=1, le=100, description="Result page (1-100)."),
    pageSize: int = Field(10, ge=1, le=50, description="Results per page (1-50).")
) -> Dict[str, Any]:
    """Search HSK İkinci Daire disiplin kararları."""
    logger.info(f"HSK search tool called with keywords: {keywords}, madde: {madde}, page: {page}")
    try:
        search_request = HskSearchRequest(keywords=keywords, madde=madde, page=page, pageSize=pageSize)
        result = await hsk_client_instance.search_decisions(search_request)
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error searching HSK decisions: {e}")
        return HskSearchResult(
            decisions=[], total_results=0, page=page, pageSize=pageSize,
            query=keywords, taranan_pdf=0
        ).model_dump()

@app.tool(
    description="Use this when retrieving full text of an HSK (Hâkimler ve Savcılar Kurulu) İkinci Daire disiplin kararı. Returns paginated Markdown. Takes uuid + sira (or hsk:disiplin:<uuid>:<sira> document_id) from search_hsk_decisions.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def get_hsk_document_markdown(
    uuid: str = Field(..., description="HSK PDF uuid from search_hsk_decisions results (e.g. 50cd1c75-...)."),
    sira: int = Field(1, ge=1, description="Karar sırası within the PDF (1-indexed) from search_hsk_decisions."),
    madde: Annotated[Optional[str], Field(description="Yaptırım maddesi başlığı (opsiyonel, görüntüleme için).")] = None,
    fikra: Annotated[Optional[str], Field(description="Fıkra kodu (opsiyonel, görüntüleme için).")] = None,
    page_number: int = Field(1, ge=1, description="Page number for paginated Markdown content (1-indexed). Default 1.")
) -> Dict[str, Any]:
    """Retrieve full text of an HSK İkinci Daire disiplin kararı."""
    logger.info(f"HSK document tool called with uuid: {uuid}, sira: {sira}, page: {page_number}")
    try:
        document = await hsk_client_instance.get_document(
            uuid=uuid, sira=sira, madde=madde, fikra=fikra, page_number=page_number
        )
        return document.model_dump()
    except Exception as e:
        logger.exception(f"Error retrieving HSK document: {e}")
        return HskDocumentMarkdown(
            source_id=f"hsk:disiplin:{uuid}:{sira}",
            source_url=f"https://www.hsk.gov.tr/Eklentiler/Dosyalar/{uuid}.pdf",
            madde=madde, fikra=fikra,
            markdown_chunk=None, current_page=page_number,
            total_pages=0, is_paginated=False,
            error_message=f"Error retrieving HSK document: {str(e)}"
        ).model_dump()

# --- MCP Tools for Reklam Kurulu (Ticaret Bakanlığı) ---
@app.tool(
    description="Use this when listing Reklam Kurulu (Ticaret Bakanlığı) basın bültenleri. Returns bulletin summaries (toplantı no, tarih, yıl) with pdf_url and document_id (reklam:<no>) usable with get_reklam_bulteni_markdown / search_reklam_bulten_ici. Optional year filter (2012-2026).",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def list_reklam_bultenleri(
    yil: Optional[int] = Field(None, ge=2012, le=2030, description="Bülten yılı filtresi (2012-2026). Boş = tüm yıllar.")
) -> Dict[str, Any]:
    """List Reklam Kurulu basın bültenleri."""
    logger.info(f"Reklam bülten list tool called for year: {yil}")
    result = await reklam_client_instance.list_bultenler(yil)
    return result.model_dump()

@app.tool(
    description="Use this when searching INSIDE one Reklam Kurulu (Ticaret Bakanlığı) basın bülteni by keyword. Downloads the bulletin (PDF/DOCX), splits it into individual decisions (Dosya No boundaries) and returns matching decisions with excerpts sorted by relevance. First call per bülten downloads and converts (large PDFs can be slow); later calls come from cache.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_reklam_bulten_ici(
    toplanti_no: int = Field(..., description="Toplantı (bülten) numarası, e.g. 370 (use list_reklam_bultenleri to find it)."),
    keywords: str = Field(..., description="Turkish keyword(s) to search inside the bulletin (e.g. 'trafik sigortası', 'kozmetik')."),
    max_results: int = Field(10, ge=1, le=25, description="Maximum matching decisions to return (default 10, max 25).")
) -> Dict[str, Any]:
    """Search inside a Reklam Kurulu bulletin for keywords."""
    logger.info(f"Reklam bülten içi arama called: bülten {toplanti_no}, keywords: {keywords}")
    try:
        result = await reklam_client_instance.search_bulten_ici(
            toplanti_no=toplanti_no, keywords=keywords, max_results=max_results
        )
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error searching Reklam bülten içi: {e}")
        return ReklamBultenIciAramaSonucu(
            toplanti_no=toplanti_no, keywords=keywords, total=0,
            kararlar=[], error_message=f"Error searching bulletin: {str(e)}"
        ).model_dump()

@app.tool(
    description="Use this when retrieving full text of a Reklam Kurulu (Ticaret Bakanlığı) basın bülteni as Markdown. Returns paginated content (5,000 chars per page). Takes the toplanti_no from list_reklam_bultenleri.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def get_reklam_bulteni_markdown(
    toplanti_no: int = Field(..., description="Toplantı (bülten) numarası, e.g. 370 (use list_reklam_bultenleri to find it)."),
    page_number: int = Field(1, ge=1, description="Page number for paginated Markdown content (1-indexed). Default 1.")
) -> Dict[str, Any]:
    """Retrieve full text of a Reklam Kurulu basın bülteni."""
    logger.info(f"Reklam bülten markdown tool called: {toplanti_no}, page: {page_number}")
    try:
        result = await reklam_client_instance.get_bulten_markdown(toplanti_no=toplanti_no, page_number=page_number)
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error retrieving Reklam bülten: {e}")
        return ReklamBultenMarkdown(
            source_id=f"reklam:{toplanti_no}", source_url=None,
            toplanti_no=toplanti_no, toplanti_tarihi=None, karar_sayisi=None,
            markdown_chunk=None, current_page=page_number,
            total_pages=0, is_paginated=False,
            error_message=f"Error retrieving bulletin: {str(e)}"
        ).model_dump()

# --- MCP Tools for BDDK (Banking Regulation Authority) ---
@app.tool(
    description="Use this when searching Turkish banking regulation (BDDK) decisions. For banking licenses, fintech, and payment services.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_bddk_decisions(
    keywords: str = Field(..., description="Search keywords in Turkish"),
    page: int = Field(1, ge=1, description="Page number")
    # pageSize: int = Field(10, ge=1, le=50, description="Results per page")
) -> dict:
    """Search BDDK banking regulation and supervision decisions."""
    logger.info(f"BDDK search tool called with keywords: {keywords}, page: {page}")
    
    pageSize = 10  # Default value
    
    try:
        search_request = BddkSearchRequest(
            keywords=keywords,
            page=page,
            pageSize=pageSize
        )
        
        result = await bddk_client_instance.search_decisions(search_request)
        logger.info(f"BDDK search completed. Found {len(result.decisions)} decisions on page {page}")
        
        return {
            "decisions": [
                {
                    "title": dec.title,
                    "document_id": dec.document_id,
                    "content": dec.content
                }
                for dec in result.decisions
            ],
            "total_results": result.total_results,
            "page": result.page,
            "pageSize": result.pageSize
        }
    
    except Exception as e:
        logger.exception(f"Error searching BDDK decisions: {e}")
        return {
            "decisions": [],
            "total_results": 0,
            "page": page,
            "pageSize": pageSize,
            "error": str(e)
        }

@app.tool(
    description="Use this when retrieving full text of a BDDK banking regulation decision. Returns paginated Markdown format.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_bddk_document_markdown(
    document_id: str = Field(..., description="BDDK document ID (e.g., '310')"),
    page_number: int = Field(1, ge=1, description="Page number")
) -> dict:
    """Retrieve BDDK decision document in Markdown format."""
    logger.info(f"BDDK document retrieval tool called for ID: {document_id}, page: {page_number}")
    
    if not document_id or not document_id.strip():
        return {
            "document_id": document_id,
            "markdown_content": "",
            "page_number": page_number,
            "total_pages": 0,
            "error": "Document ID is required"
        }
    
    try:
        result = await bddk_client_instance.get_document_markdown(document_id, page_number)
        logger.info(f"BDDK document retrieved successfully. Page {result.page_number}/{result.total_pages}")
        
        return {
            "document_id": result.document_id,
            "markdown_content": result.markdown_content,
            "page_number": result.page_number,
            "total_pages": result.total_pages
        }
        
    except Exception as e:
        logger.exception(f"Error retrieving BDDK document: {e}")
        return {
            "document_id": document_id,
            "markdown_content": "",
            "page_number": page_number,
            "total_pages": 0,
            "error": str(e)
        }

# --- MCP Tools for BTK (Information and Communication Technologies Authority) ---
@app.tool(
    description=(
        "Use this when searching BTK Board decisions (Bilgi Teknolojileri ve Iletisim Kurumu Kurul Kararlari). "
        "Supports decision title keywords, decision number, decision date, publication date, and related department filters."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_btk_decisions(
    keywords: str = Field("", description="Keywords searched by BTK's official search endpoint."),
    decision_no: str = Field("", description="Decision number, e.g. 2026/DK-THD/91."),
    decision_date: str = Field("", description="Decision date as YYYY-MM-DD."),
    publication_date: str = Field("", description="Publication date as YYYY-MM-DD."),
    relevant_unit: str = Field("", description="Related BTK department name."),
    page: int = Field(1, ge=1, description="Page number."),
    pageSize: int = Field(10, ge=1, le=50, description="Results per page.")
) -> Dict[str, Any]:
    """Search BTK Board decisions."""
    logger.info(
        "BTK search tool called with keywords=%s, decision_no=%s, page=%s",
        keywords,
        decision_no,
        page,
    )

    search_request = BtkSearchRequest(
        keywords=keywords,
        decision_no=decision_no,
        decision_date=decision_date,
        publication_date=publication_date,
        relevant_unit=relevant_unit,
        page=page,
        pageSize=pageSize,
    )

    try:
        result = await btk_client_instance.search_decisions(search_request)
        logger.info("BTK search completed. Found %s decisions on page %s", len(result.decisions), page)
        return result.model_dump()
    except Exception as e:
        logger.exception("Error searching BTK decisions: %s", e)
        return BtkSearchResult(
            decisions=[],
            total_results=0,
            page=page,
            pageSize=pageSize,
            total_pages=0,
            query_url=""
        ).model_dump()

@app.tool(
    description="Use this when retrieving full text of a BTK Board decision PDF. Returns paginated Markdown.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_btk_document_markdown(
    pdf_url: str = Field(..., description="Direct BTK PDF URL returned by search_btk_decisions in the pdf_url field."),
    page_number: int = Field(1, ge=1, description="Page number for paginated Markdown content. Each page is about 5,000 characters.")
) -> Dict[str, Any]:
    """Retrieve a BTK decision PDF as paginated Markdown."""
    logger.info("BTK document retrieval tool called for URL: %s, page: %s", pdf_url, page_number)

    if not pdf_url or not pdf_url.strip():
        return BtkDocumentMarkdown(
            source_url=HttpUrl("https://www.btk.tr/kurul-kararlari"),
            markdown_chunk=None,
            current_page=page_number or 1,
            total_pages=0,
            is_paginated=False,
            error_message="pdf_url is required and cannot be empty."
        ).model_dump()

    try:
        result = await btk_client_instance.get_document_markdown(pdf_url, page_number or 1)
        logger.info("BTK document retrieved. Page %s/%s", result.current_page, result.total_pages)
        return result.model_dump()
    except Exception as e:
        logger.exception("Error retrieving BTK document: %s", e)
        return BtkDocumentMarkdown(
            source_url=HttpUrl(pdf_url),
            markdown_chunk=None,
            current_page=page_number or 1,
            total_pages=0,
            is_paginated=False,
            error_message=f"Error retrieving BTK document: {str(e)}"
        ).model_dump()

@app.tool(
    description=(
        "Search Turkish GİB özelge records (Revenue Administration tax rulings) - 18k+ rulings on VAT, "
        "income tax, corporate tax, stamp duty interpretations. kanunNo filters the related law number; "
        "returned documents are özelge/tax ruling records."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_gib_ozelge(
    keywords: str = Field("", description="Turkish keywords searched in title, kanunNo and description (e.g., 'KDV oranı', 'kurumlar vergisi istisna')"),
    ozelgeNo: str = Field("", description="Exact özelge reference number (e.g., 'E-40247694-130-15524')"),
    kanunNo: str = Field("", description="Related law number filter for özelge records, e.g. '3065' for KDV, '193' for Gelir Vergisi"),
    ozelgeStartDate: str = Field("", description="Start date YYYY-MM-DD (e.g., '2024-01-01') or full ISO 8601"),
    ozelgeEndDate: str = Field("", description="End date YYYY-MM-DD (e.g., '2024-12-31') or full ISO 8601"),
    page: int = Field(1, ge=1, description="Page number (1-indexed)"),
    pageSize: int = Field(10, ge=1, le=50, description="Results per page (1-50)")
) -> dict:
    """Search GİB özelgeler (Turkish Revenue Administration tax rulings)."""
    logger.info(
        f"GİB search tool called with keywords='{keywords}', ozelgeNo='{ozelgeNo}', "
        f"kanunNo='{kanunNo}', start={ozelgeStartDate}, end={ozelgeEndDate}, "
        f"page={page}, pageSize={pageSize}"
    )

    try:
        search_request = GibSearchRequest(
            keywords=keywords,
            ozelgeNo=ozelgeNo,
            kanunNo=kanunNo,
            ozelgeStartDate=ozelgeStartDate,
            ozelgeEndDate=ozelgeEndDate,
            page=page,
            pageSize=pageSize,
        )
        result = await gib_client_instance.search_ozelge(search_request)
        logger.info(
            f"GİB search completed. Found {len(result.ozelgeler)} rulings on page {page} "
            f"(total {result.total_results})"
        )
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error searching GİB özelgeler: {e}")
        return GibSearchResult(
            ozelgeler=[],
            total_results=0,
            total_pages=0,
            current_page=page,
            page_size=pageSize,
        ).model_dump()


@app.tool(
    description="Retrieve full text of a GİB özelge (tax ruling) by numeric ID. Returns paginated Markdown (5000-char chunks) with title, reference number, date and law metadata.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_gib_ozelge_document_markdown(
    ozelge_id: int = Field(..., ge=1, description="Numeric özelge ID from search results (e.g., 38849)"),
    page_number: int = Field(1, ge=1, description="Page number for paginated Markdown (1-indexed)")
) -> dict:
    """Retrieve a GİB özelge document in paginated Markdown format."""
    logger.info(f"GİB document retrieval tool called for id={ozelge_id}, page={page_number}")

    try:
        result = await gib_client_instance.get_ozelge_document(ozelge_id, page_number)
        logger.info(
            f"GİB document retrieved. id={ozelge_id} page={result.current_page}/{result.total_pages}"
        )
        return result.model_dump()
    except Exception as e:
        logger.exception(f"Error retrieving GİB document: {e}")
        return GibDocumentMarkdown(
            ozelge_id=ozelge_id,
            current_page=page_number,
            total_pages=0,
            is_paginated=False,
            error_message=str(e),
        ).model_dump()


# --- MCP Tools for Sigorta Tahkim Komisyonu (Insurance Arbitration Commission) ---
@app.tool(
    description="Search Sigorta Tahkim Komisyonu (Insurance Arbitration Commission) decisions from Hakem Karar Dergisi journals (64 issues, 2010-2025). Covers insurance disputes: traffic, health, fire, DASK, life insurance.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search_sigorta_tahkim_decisions(
    keywords: str = Field(..., description="Search keywords in Turkish (e.g., 'trafik sigortası', 'kasko', 'DASK')"),
    page: int = Field(1, ge=1, description="Page number")
) -> dict:
    """Search Sigorta Tahkim Komisyonu insurance arbitration decisions."""
    logger.info(f"Sigorta Tahkim search tool called with keywords: {keywords}, page: {page}")

    pageSize = 10

    try:
        search_request = SigortaTahkimSearchRequest(
            keywords=keywords,
            page=page,
            pageSize=pageSize
        )

        result = await sigorta_tahkim_client_instance.search_decisions(search_request)
        logger.info(f"Sigorta Tahkim search completed. Found {len(result.decisions)} results on page {page}")

        return {
            "decisions": [
                {
                    "title": dec.title,
                    "document_id": dec.document_id,
                    "content": dec.content,
                    "url": dec.url
                }
                for dec in result.decisions
            ],
            "total_results": result.total_results,
            "page": result.page,
            "pageSize": result.pageSize
        }

    except Exception as e:
        logger.exception(f"Error searching Sigorta Tahkim decisions: {e}")
        return {
            "decisions": [],
            "total_results": 0,
            "page": page,
            "pageSize": pageSize,
            "error": str(e)
        }

@app.tool(
    description="Retrieve full PDF content of a Sigorta Tahkim Komisyonu Hakem Karar Dergisi issue by number. Returns paginated Markdown. Issues 1-64 available (2010-2025).",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_sigorta_tahkim_document_markdown(
    issue_number: str = Field(..., description="Journal issue number (1-64, e.g., '64')"),
    page_number: int = Field(1, ge=1, description="Page number for paginated content")
) -> dict:
    """Retrieve Sigorta Tahkim journal issue PDF as paginated Markdown."""
    logger.info(f"Sigorta Tahkim document retrieval for issue: {issue_number}, page: {page_number}")

    if not issue_number or not issue_number.strip():
        return {
            "document_id": issue_number,
            "markdown_content": "",
            "page_number": page_number,
            "total_pages": 0,
            "source_url": "",
            "error": "Issue number is required"
        }

    try:
        result = await sigorta_tahkim_client_instance.get_document_markdown(issue_number, page_number)
        logger.info(f"Sigorta Tahkim document retrieved. Page {result.page_number}/{result.total_pages}")

        return {
            "document_id": result.document_id,
            "markdown_content": result.markdown_content,
            "page_number": result.page_number,
            "total_pages": result.total_pages,
            "source_url": result.source_url
        }

    except Exception as e:
        logger.exception(f"Error retrieving Sigorta Tahkim document: {e}")
        return {
            "document_id": issue_number,
            "markdown_content": "",
            "page_number": page_number,
            "total_pages": 0,
            "source_url": "",
            "error": str(e)
        }

@app.tool(
    description="Search within a specific Sigorta Tahkim Komisyonu journal issue for keywords. Downloads the PDF, splits into individual decisions, and returns matching decisions with excerpts sorted by relevance.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def search_within_sigorta_tahkim_issue(
    issue_number: str = Field(..., description="Journal issue number (1-64, e.g., '64')"),
    keyword: str = Field(..., description="Search keyword in Turkish (e.g., 'trafik kazası', 'tazminat')"),
    max_results: int = Field(10, ge=1, le=25, description="Max matching decisions to return")
) -> dict:
    """Search for keywords within a specific Sigorta Tahkim journal issue's decisions."""
    logger.info(f"Sigorta Tahkim search_within called: issue={issue_number}, keyword={keyword}")

    if not issue_number or not issue_number.strip():
        return {"issue_number": issue_number, "keyword": keyword, "matches": [], "error": "Issue number is required"}
    if not keyword or not keyword.strip():
        return {"issue_number": issue_number, "keyword": keyword, "matches": [], "error": "Keyword is required"}

    try:
        result = await sigorta_tahkim_client_instance.search_within_issue(
            issue_number, keyword, max_results
        )
        logger.info(
            f"Sigorta Tahkim search_within completed: "
            f"{result.matching_decisions}/{result.total_decisions} decisions match"
        )

        return {
            "issue_number": result.issue_number,
            "keyword": result.keyword,
            "total_decisions": result.total_decisions,
            "matching_decisions": result.matching_decisions,
            "matches": [
                {
                    "decision_header": m.decision_header,
                    "relevance_score": m.relevance_score,
                    "excerpt": m.excerpt,
                    "body_length": m.body_length
                }
                for m in result.matches
            ]
        }

    except Exception as e:
        logger.exception(f"Error in search_within Sigorta Tahkim: {e}")
        return {
            "issue_number": issue_number,
            "keyword": keyword,
            "total_decisions": 0,
            "matching_decisions": 0,
            "matches": [],
            "error": str(e)
        }

# --- ChatGPT Deep Research Compatible Tools ---

def build_bedesten_title(decision: Any, court_name: str) -> str:
    """Build a compact title from Bedesten search metadata without fetching the document."""
    title_parts = [court_name]
    if getattr(decision, "birimAdi", None):
        title_parts.append(str(decision.birimAdi))
    if getattr(decision, "esasNo", None):
        title_parts.append(f"Esas: {decision.esasNo}")
    if getattr(decision, "kararNo", None):
        title_parts.append(f"Karar: {decision.kararNo}")
    if getattr(decision, "kararTarihiStr", None):
        title_parts.append(f"Tarih: {decision.kararTarihiStr}")
    return " - ".join(title_parts) if title_parts else f"{court_name} - Document {decision.documentId}"


def build_bedesten_metadata_preview(decision: Any, court_name: str) -> str:
    """Return Deep Research preview text using only search-result metadata."""
    preview_parts = [f"Kaynak: {court_name}"]
    if getattr(decision, "birimAdi", None):
        preview_parts.append(f"Daire/Kurul: {decision.birimAdi}")
    if getattr(decision, "esasNo", None):
        preview_parts.append(f"Esas No: {decision.esasNo}")
    if getattr(decision, "kararNo", None):
        preview_parts.append(f"Karar No: {decision.kararNo}")
    if getattr(decision, "kararTarihiStr", None):
        preview_parts.append(f"Karar Tarihi: {decision.kararTarihiStr}")
    preview_parts.append("Tam metin için fetch aracını bu sonucun id değeriyle çağırın.")
    return ". ".join(preview_parts)


@app.tool(
    description=(
        "Only for ChatGPT Deep Research. Searches Bedesten-supported Turkish court databases and returns "
        "OpenAI Deep Research compatible results (id, title, text, url). For regular MCP use, prefer "
        "search_bedesten_unified. This tool does not fetch document bodies during search so one query stays "
        "within Bedesten upstream rate limits; call fetch only for selected result IDs."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def search(
    query: str = Field(..., description="Turkish search query")
) -> Dict[str, Any]:
    """
    Bedesten API search tool for ChatGPT Deep Research compatibility.
    
    This tool searches Turkish legal databases via the unified Bedesten API.
    It supports advanced search operators and covers all major court types.
    
    USAGE RESTRICTION: Only for ChatGPT Deep Research workflows.
    For regular legal research, use search_bedesten_unified with specific court types.
    
    Returns:
    Object with "results" field containing a list of documents with id, title, text preview, and url
    as required by ChatGPT Deep Research specification.
    """
    logger.info(f"ChatGPT Deep Research search tool called with query: {query}")
    
    results = []
    
    try:
        # Search all court types via unified Bedesten API
        court_types = [
            ("YARGITAYKARARI", "Yargıtay"),
            ("DANISTAYKARAR", "Danıştay"),
            ("YERELHUKUK", "Yerel Hukuk Mahkemesi"),
            ("ISTINAFHUKUK", "İstinaf Hukuk Mahkemesi"),
            ("KYB", "Kanun Yararına Bozma")
        ]
        
        for item_type, court_name in court_types:
            try:
                search_results = await bedesten_client_instance.search_documents(
                    BedestenSearchRequest(
                        data=BedestenSearchData(
                            phrase=query,  # Use query as-is to support both regular and exact phrase searches
                            itemTypeList=[item_type],
                            pageSize=5,
                            pageNumber=1
                        )
                    )
                )
                
                # Handle potential None data
                if search_results.data is None:
                    logger.warning(f"No data returned from Bedesten API for {court_name}")
                    continue
                
                # Add results from metadata only. Fetching every document preview
                # would turn one Deep Research search into ~30 Bedesten requests.
                for decision in search_results.data.emsalKararList[:5]:
                    results.append({
                        "id": decision.documentId,
                        "title": build_bedesten_title(decision, court_name),
                        "text": build_bedesten_metadata_preview(decision, court_name),
                        "url": f"https://mevzuat.adalet.gov.tr/ictihat/{decision.documentId}"
                    })
                    
                if search_results.data:
                    logger.info(f"Found {len(search_results.data.emsalKararList)} results from {court_name}")
                else:
                    logger.info(f"Found 0 results from {court_name} (no data returned)")
                
            except Exception as e:
                logger.warning(f"Bedesten API search error for {court_name}: {e}")
        
        # Comment out other API implementations for ChatGPT Deep Research
        """
        # Other API implementations disabled for ChatGPT Deep Research
        # These are available through specific court tools:
        
        # Yargıtay Official API - use search_yargitay_detailed instead
        # Danıştay Official API - use search_danistay_by_keyword instead  
        # Constitutional Court - use search_anayasa_norm_denetimi_decisions instead
        # Competition Authority - use search_rekabet_kurumu_decisions instead
        # Public Procurement Authority - use search_kik_v2_decisions instead
        # Court of Accounts - use search_sayistay_* tools instead
        # UYAP Emsal - use search_emsal_detailed_decisions instead
        # Jurisdictional Disputes Court - use search_uyusmazlik_decisions instead
        """
        
        logger.info(f"ChatGPT Deep Research search completed. Found {len(results)} results via Bedesten API.")
        return {
            "results": [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "text": item["text"],
                    "url": item["url"]
                }
                for item in results
            ]
        }
        
    except Exception:
        logger.exception("Error in ChatGPT Deep Research search tool")
        # Return partial results if any were found
        if results:
            return {
                "results": [
                    {
                        "id": item["id"],
                        "title": item["title"],
                        "text": item["text"],
                        "url": item["url"]
                    }
                    for item in results
                ]
            }
        raise

@app.tool(
    description=(
        "Only for ChatGPT Deep Research. Retrieves one Turkish legal document by numeric Bedesten ID. "
        "For regular MCP use, prefer get_bedesten_document_markdown. This performs one Bedesten document request "
        "and avoids an extra metadata lookup to respect upstream rate limits."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,  # Retrieves specific documents, not exploring
        "idempotentHint": True
    }
)
async def fetch(
    id: str = Field(..., description="Document identifier from search results (numeric only)")
) -> Dict[str, Any]:
    """
    Bedesten API fetch tool for ChatGPT Deep Research compatibility.
    
    Retrieves the full text content of Turkish legal documents via unified Bedesten API.
    Converts documents from HTML/PDF to clean Markdown format.
    
    USAGE RESTRICTION: Only for ChatGPT Deep Research workflows.
    For regular legal research, use specific court document tools.
    
    Input Format:
    - id: Numeric document identifier from search results (e.g., "730113500", "71370900")
    
    Returns:
    Single object with numeric id, title, text (full Markdown content), mevzuat.adalet.gov.tr url, and metadata fields
    as required by ChatGPT Deep Research specification.
    """
    logger.info(f"ChatGPT Deep Research fetch tool called for document ID: {id}")
    
    if not id or not id.strip():
        raise ValueError("Document ID must be a non-empty string")
    
    try:
        # Use the numeric ID directly with Bedesten API
        doc = await bedesten_client_instance.get_document_as_markdown(id)
        
        title = f"Turkish Legal Document {id}"
        if doc.markdown_content:
            for line in doc.markdown_content.splitlines():
                cleaned_line = line.strip().lstrip("#").strip()
                if cleaned_line:
                    title = cleaned_line[:160]
                    break
        
        return {
            "id": id,
            "title": title,
            "text": doc.markdown_content,
            "url": f"https://mevzuat.adalet.gov.tr/ictihat/{id}",
            "metadata": {
                "database": "Turkish Legal Database via Bedesten API",
                "document_id": id,
                "source_url": doc.source_url,
                "mime_type": doc.mime_type,
                "api_source": "Bedesten Unified API",
                "chatgpt_deep_research": True,
                "rate_limit_optimized": True
            }
        }
        
        # Comment out other API implementations for ChatGPT Deep Research
        """
        # Other API implementations disabled for ChatGPT Deep Research
        # These are available through specific court document tools:
        
        elif id.startswith("yargitay_"):
            # Yargıtay Official API - use get_yargitay_document_markdown instead
            doc_id = id.replace("yargitay_", "")
            doc = await yargitay_client_instance.get_decision_document_as_markdown(doc_id)
            
        elif id.startswith("danistay_"):
            # Danıştay Official API - use get_danistay_document_markdown instead
            doc_id = id.replace("danistay_", "")
            doc = await danistay_client_instance.get_decision_document_as_markdown(doc_id)
            
        elif id.startswith("anayasa_"):
            # Constitutional Court - use get_anayasa_norm_denetimi_document_markdown instead
            doc_id = id.replace("anayasa_", "")
            doc = await anayasa_norm_client_instance.get_decision_document_as_markdown(...)
            
        elif id.startswith("rekabet_"):
            # Competition Authority - use get_rekabet_kurumu_document instead
            doc_id = id.replace("rekabet_", "")
            doc = await rekabet_client_instance.get_decision_document(...)
            
        elif id.startswith("kik_"):
            # Public Procurement Authority - use get_kik_decision_document_as_markdown instead
            doc_id = id.replace("kik_", "")
            doc = await kik_client_instance.get_decision_document_as_markdown(doc_id)
            
        elif id.startswith("local_"):
            # This was already using Bedesten API, but deprecated for ChatGPT Deep Research
            doc_id = id.replace("local_", "")
            doc = await bedesten_client_instance.get_document_as_markdown(doc_id)
        """
        
    except Exception:
        logger.exception(f"Error fetching ChatGPT Deep Research document {id}")
        raise

# --- Token Metrics Tool Removed for Optimization ---

# --- Mevzuat Module Tools (Bedesten legislation API) ---

@app.tool(
    description=(
        "Search Turkish legislation (Mevzuat) via the official Adalet Bakanlığı Bedesten API. "
        "Searches legislation titles (e.g. kanun, yönetmelik, tebliğ). Optionally filter by "
        "legislation type (KANUN, YONETMELIK, TEBLIG, CUMHURBASKANLIGI KARARI...), legislation "
        "number (e.g. 5237) and publication date range. Returns records with mevzuat_id, number, "
        "title, type, gazette date/issue and canonical mevzuat.gov.tr URL. Use get_mevzuat_document "
        "to retrieve the full text."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def mevzuat_ara(
    query: str = Field("", description="Keywords searched in legislation titles, e.g. 'haksız rekabet' or 'vergi'."),
    mevzuat_turu: str = Field("", description="Legislation type filter, e.g. 'KANUN', 'YONETMELIK', 'TEBLIG'. Empty for all types."),
    mevzuat_no: str = Field("", description="Legislation number filter, e.g. '5237' (Türk Ceza Kanunu)."),
    baslangic_tarihi: str = Field("", description="Start date YYYY-MM-DD of publication in the Official Gazette."),
    bitis_tarihi: str = Field("", description="End date YYYY-MM-DD of publication in the Official Gazette."),
    sayfa: int = Field(1, ge=1, description="Page number."),
    sayfa_boyutu: int = Field(10, ge=1, le=50, description="Results per page.")
) -> Dict[str, Any]:
    """Search Turkish legislation via Bedesten API."""
    logger.info("mevzuat_ara called with query=%s, tur=%s, no=%s", query, mevzuat_turu, mevzuat_no)
    request = MevzuatSearchRequest(
        mevzuatAdi=query,
        mevzuatNo=mevzuat_no,
        mevzuatTurList=[mevzuat_turu] if mevzuat_turu.strip() else [],
        resmiGazeteTarihiStart=baslangic_tarihi,
        resmiGazeteTarihiEnd=bitis_tarihi,
        page=sayfa,
        pageSize=sayfa_boyutu,
    )
    try:
        result = await mevzuat_client_instance.search_mevzuat(request)
        logger.info("mevzuat_ara found %s results", result.total_results)
        return result.model_dump()
    except Exception as e:
        logger.exception("mevzuat_ara error: %s", e)
        return {"results": [], "total_results": 0, "page": sayfa, "pageSize": sayfa_boyutu}


@app.tool(
    description=(
        "Retrieve the full text of a Turkish legislation document as paginated Markdown. "
        "Takes the mevzuat_id returned by mevzuat_ara. Returns the complete law/regulation "
        "text in 5,000-character pages."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_mevzuat_document(
    mevzuat_id: str = Field(..., description="Internal document ID from mevzuat_ara results (e.g. '104695')."),
    sayfa_no: int = Field(1, ge=1, description="Page number for paginated Markdown content (5,000 chars per page).")
) -> Dict[str, Any]:
    """Retrieve a legislation document as paginated Markdown."""
    logger.info("get_mevzuat_document called with mevzuat_id=%s, page=%s", mevzuat_id, sayfa_no)
    if not mevzuat_id or not mevzuat_id.strip():
        return {"mevzuat_id": mevzuat_id, "error_message": "mevzuat_id is required."}
    try:
        result = await mevzuat_client_instance.get_mevzuat_markdown(mevzuat_id, sayfa_no)
        return result.model_dump()
    except Exception as e:
        logger.exception("get_mevzuat_document error: %s", e)
        return {"mevzuat_id": mevzuat_id, "error_message": f"Error: {str(e)}"}


@app.tool(
    description=(
        "Search a keyword inside a specific legislation's full text (mevzuat icinde ara). "
        "Takes the mevzuat_id from mevzuat_ara and returns context snippets around each "
        "occurrence of the keyword with a total match count."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def mevzuat_icinde_ara(
    mevzuat_id: str = Field(..., description="Internal document ID from mevzuat_ara results (e.g. '104695')."),
    anahtar_kelime: str = Field(..., description="Keyword to search inside the legislation text."),
    maksimum_eslesme: int = Field(10, ge=1, le=50, description="Maximum number of context snippets to return.")
) -> Dict[str, Any]:
    """Search a keyword inside a legislation's full text."""
    logger.info("mevzuat_icinde_ara called with mevzuat_id=%s, keyword=%s", mevzuat_id, anahtar_kelime)
    if not mevzuat_id or not anahtar_kelime:
        return {"mevzuat_id": mevzuat_id, "match_count": 0, "snippets": [], "error_message": "mevzuat_id and anahtar_kelime are required."}
    try:
        result = await mevzuat_client_instance.search_within_mevzuat(mevzuat_id, anahtar_kelime, maksimum_eslesme)
        return result.model_dump()
    except Exception as e:
        logger.exception("mevzuat_icinde_ara error: %s", e)
        return {"mevzuat_id": mevzuat_id, "match_count": 0, "snippets": [], "error_message": f"Error: {str(e)}"}


# --- Resmî Gazete Module Tools ---

@app.tool(
    description=(
        "Get the fihrist (table of contents) of the Turkish Official Gazette (Resmî Gazete) "
        "for a specific date. Returns sections (bölümler) with article titles and URLs "
        "(.htm or .pdf). Mükerrer issues of the same date are also returned when present."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def resmi_gazete_fihrist(
    tarih: str = Field(..., description="Date in YYYY-MM-DD format, e.g. '2026-08-01'."),
    mukerrer_dahil: bool = Field(True, description="Include mükerrer (extra) issues of the same date.")
) -> Dict[str, Any]:
    """Get the Official Gazette fihrist for a date."""
    logger.info("resmi_gazete_fihrist called with date=%s", tarih)
    try:
        result = await resmi_gazete_client_instance.get_fihrist(tarih, mukerrer_dahil)
        return result.model_dump()
    except Exception as e:
        logger.exception("resmi_gazete_fihrist error: %s", e)
        return {"date": tarih, "error_message": f"Error: {str(e)}"}


@app.tool(
    description=(
        "Retrieve a single Resmî Gazete article (.htm or .pdf) as Markdown text. "
        "Takes the article URL from resmi_gazete_fihrist results. PDF articles are "
        "converted to text via pypdf."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def resmi_gazete_madde_getir(
    url: str = Field(..., description="Article URL from resmi_gazete_fihrist, e.g. 'https://www.resmigazete.gov.tr/eskiler/2026/08/20260801-2.htm'.")
) -> Dict[str, Any]:
    """Retrieve a Resmî Gazete article as Markdown."""
    logger.info("resmi_gazete_madde_getir called with url=%s", url)
    if not url or not url.strip():
        return {"url": url, "error_message": "url is required."}
    try:
        result = await resmi_gazete_client_instance.get_article_markdown(url)
        return result.model_dump()
    except Exception as e:
        logger.exception("resmi_gazete_madde_getir error: %s", e)
        return {"url": url, "error_message": f"Error: {str(e)}"}


@app.tool(
    description=(
        "Search Resmî Gazete fihrist titles across a date range for a keyword. "
        "Scans each published day's fihrist (Sundays are skipped; Resmî Gazete is not "
        "published on Sundays) and returns matching article titles with URLs and context "
        "snippets. Turkish characters are normalized for matching."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def resmi_gazete_ara(
    query: str = Field(..., description="Keyword to search in fihrist titles, e.g. 'tebliğ' or 'yönetmelik'."),
    fromDate: str = Field(..., description="Start date YYYY-MM-DD."),
    toDate: str = Field(..., description="End date YYYY-MM-DD (max range 730 days)."),
    maxResults: int = Field(50, ge=1, le=500, description="Maximum number of matches to return.")
) -> Dict[str, Any]:
    """Search Resmî Gazete fihrist titles over a date range."""
    logger.info("resmi_gazete_ara called with query=%s, %s..%s", query, fromDate, toDate)
    try:
        result = await resmi_gazete_client_instance.search_fihrist_titles(query, fromDate, toDate, maxResults)
        return result.model_dump()
    except Exception as e:
        logger.exception("resmi_gazete_ara error: %s", e)
        return {"matches": [], "error_message": f"Error: {str(e)}"}


# --- AİHM (ECHR HUDOC) Module Tools ---

@app.tool(
    description=(
        "Search European Court of Human Rights (AİHM/ECHR) case law via the official HUDOC "
        "database. Supports full-text keyword search (English terms recommended), case name, "
        "application number, ECLI, language (ENG/TUR/FRA), collection (GRANDCHAMBER/CHAMBER/COMMITTEE), "
        "importance level and judgment date range. Returns case metadata: itemid, docname, appno, "
        "decision date, conclusion, ECLI, respondent country. Use get_aihm_document to retrieve "
        "the full judgment text."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True
    }
)
async def aihm_ictihat_ara(
    keywords: str = Field("", description="Keywords searched in full text (use English legal terms, e.g. 'freedom of expression')."),
    docname: str = Field("", description="Exact case name, e.g. 'A.G. v. SWITZERLAND'."),
    appno: str = Field("", description="Application number, e.g. '15345/20'."),
    ecli: str = Field("", description="ECLI identifier, e.g. 'ECLI:CE:ECHR:2026:0723JUD001534520'."),
    dil: str = Field("ENG", description="Language ISO code: ENG (English), TUR (Turkish), FRA (French)."),
    koleksiyon: str = Field("", description="Document collection: GRANDCHAMBER, CHAMBER, COMMITTEE, ADMISSIBILITY. Empty for all."),
    onem: int = Field(0, ge=0, le=4, description="Importance: 0=all, 1=Key case (önemli karar), 4=lowest."),
    baslangic_tarihi: str = Field("", description="Judgment date start YYYY-MM-DD."),
    bitis_tarihi: str = Field("", description="Judgment date end YYYY-MM-DD."),
    sayfa: int = Field(1, ge=1, description="Page number."),
    sayfa_boyutu: int = Field(10, ge=1, le=50, description="Results per page.")
) -> Dict[str, Any]:
    """Search ECHR case law via HUDOC."""
    logger.info("aihm_ictihat_ara called with keywords=%s, collection=%s", keywords, koleksiyon)
    request = AihmSearchRequest(
        keywords=keywords,
        docname=docname,
        appno=appno,
        ecli=ecli,
        language=dil,
        collection=koleksiyon,
        importance=onem,
        date_from=baslangic_tarihi,
        date_to=bitis_tarihi,
        page=sayfa,
        pageSize=sayfa_boyutu,
    )
    try:
        result = await aihm_client_instance.search_cases(request)
        logger.info("aihm_ictihat_ara found %s results", result.total_results)
        return result.model_dump()
    except Exception as e:
        logger.exception("aihm_ictihat_ara error: %s", e)
        return {"results": [], "total_results": 0, "page": sayfa, "pageSize": sayfa_boyutu}


@app.tool(
    description=(
        "Retrieve the full text of an ECHR (AİHM) judgment as paginated Markdown. "
        "Takes the itemid returned by aihm_ictihat_ara. Returns the complete judgment text "
        "in 5,000-character pages."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True
    }
)
async def get_aihm_document(
    itemid: str = Field(..., description="HUDOC item ID from aihm_ictihat_ara results (e.g. '001-251506')."),
    sayfa_no: int = Field(1, ge=1, description="Page number for paginated Markdown content (5,000 chars per page).")
) -> Dict[str, Any]:
    """Retrieve an ECHR judgment as paginated Markdown."""
    logger.info("get_aihm_document called with itemid=%s, page=%s", itemid, sayfa_no)
    if not itemid or not itemid.strip():
        return {"itemid": itemid, "error_message": "itemid is required."}
    try:
        result = await aihm_client_instance.get_document_markdown(itemid, sayfa_no)
        return result.model_dump()
    except Exception as e:
        logger.exception("get_aihm_document error: %s", e)
        return {"itemid": itemid, "error_message": f"Error: {str(e)}"}


def main():
    # Initialize the app properly with create_app()
    global app
    app = create_app()

    logger.info(f"Starting {app.name} server via main() function...")
    # logger.info(f"Logs will be written to: {LOG_FILE_PATH}")  # File logging disabled

    try:
        app.run()
    except KeyboardInterrupt: 
        logger.info("Server shut down by user (KeyboardInterrupt).")
    except Exception: 
        logger.exception("Server failed to start or crashed.")
    finally:
        logger.info(f"{app.name} server has shut down.")

if __name__ == "__main__": 
    main()


