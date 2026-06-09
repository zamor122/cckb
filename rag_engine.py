#!/usr/bin/env python3
import os
import sys
import json
import socket
import argparse
import operator
import boto3
import botocore.exceptions
from typing import Annotated, TypedDict, Optional, List

# LangGraph & LangChain Ollama
try:
    from langchain_ollama import OllamaLLM
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError:
    print("Warning: langgraph, langchain-ollama, or langgraph-checkpoint-sqlite not installed.")

# Helper to load CCKB env variables
def load_cckb_env():
    env_path = ".cckb/.env"
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    env_vars[k] = v
    return env_vars

# Boto3 client helper
def get_s3_client():
    cckb_env = load_cckb_env()
    minio_user = cckb_env.get("MINIO_ROOT_USER") or os.getenv("MINIO_ROOT_USER", "minioadmin")
    minio_pass = cckb_env.get("MINIO_ROOT_PASSWORD") or os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
    
    return boto3.client(
        's3',
        endpoint_url='http://localhost:9000',
        aws_access_key_id=minio_user,
        aws_secret_access_key=minio_pass,
        region_name='us-east-1'
    )

# Helper to check if Ollama is online
def is_ollama_online():
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False

# 1. State Definition
class AgentState(TypedDict):
    intent: str
    implicated_features: list[str]
    metadata_docs: list[dict]
    feature_docs: list[str]
    global_constitution: str
    final_payload: str
    clarification_message: Optional[str]
    history: Annotated[list[str], operator.add]

# 2. Nodes implementation

# Analyze_Intent Node
def analyze_intent_node(state: AgentState):
    intent = state.get("intent", "")
    history = state.get("history", [])
    
    # 1. Fetch available specs from MinIO features/ directory to know valid domains
    available_specs = []
    try:
        s3 = get_s3_client()
        response = s3.list_objects_v2(Bucket='cckb-data', Prefix='features/')
        if 'Contents' in response:
            for item in response['Contents']:
                key = item['Key']
                # e.g., features/AUTH-101.md -> AUTH-101
                if key.endswith(".md") and "/" in key:
                    spec_id = key.split("/")[-1].replace(".md", "")
                    available_specs.append(spec_id)
    except Exception as e:
        print(f"Warning: Could not fetch specs from MinIO: {e}", file=sys.stderr)
        
    if not available_specs:
        # Fallback to known specs if MinIO is unreachable
        available_specs = ["AUTH-101", "RESUME-TAILOR-PIPELINE", "MCP-TOOLS", "AI-PROVIDER"]
        
    # Check if Ollama is offline
    if not is_ollama_online():
        # Fallback router when offline — keyword matching against known spec domains
        print("Warning: Ollama container is unreachable. Running offline routing.", file=sys.stderr)
        matches = []
        
        # Direct spec ID match
        for spec in available_specs:
            if spec.lower() in intent.lower():
                matches.append(spec)
                
        # Domain keyword fallback rules
        if not matches:
            intent_lower = intent.lower()
            
            # Auth / SSO domain
            if any(kw in intent_lower for kw in ["auth", "sso", "login", "sign in", "session", "jwt", "token"]):
                if "AUTH-101" in available_specs:
                    matches.append("AUTH-101")

            # Resume tailoring / pipeline domain
            if any(kw in intent_lower for kw in [
                "tailor", "tailoring", "resume", "pipeline", "humanize", "stream",
                "ats", "score", "job description", "keyword", "bullet", "optimize",
                "improvement", "match score", "baseline", "obfuscat",
            ]):
                if "RESUME-TAILOR-PIPELINE" in available_specs:
                    matches.append("RESUME-TAILOR-PIPELINE")

            # MCP tools domain
            if any(kw in intent_lower for kw in [
                "mcp", "mcp-tool", "mcp tool", "keyword extractor", "resume parser",
                "company research", "relevancy scorer", "format recommender",
                "resume validator", "metrics context", "parallel", "deterministic tool",
            ]):
                if "MCP-TOOLS" in available_specs:
                    matches.append("MCP-TOOLS")

            # AI provider / model domain
            if any(kw in intent_lower for kw in [
                "provider", "openai", "anthropic", "gemini", "cerebras", "groq",
                "model", "llm", "fallback model", "rate limit", "api key", "byok",
                "deepseek", "mistral", "huggingface", "openrouter",
            ]):
                if "AI-PROVIDER" in available_specs:
                    matches.append("AI-PROVIDER")
                    
        return {
            "implicated_features": matches[:3],
            "clarification_message": None
        }

    # Prompt constructing conversation context
    history_context = ""
    if history:
        history_context = "\nConversation History:\n" + "\n".join([f"- User intent: {h}" for h in history])

    prompt = f"""You are a query router analyzing developer intents and mapping them to specific system feature specifications.
Available feature specifications: {available_specs}
{history_context}
Current User Intent: "{intent}"

Evaluate the current user intent. Output a JSON object mapping this intent to relevant feature specs.
The response MUST strictly follow this JSON format:
{{
  "implicated_features": ["SPEC-ID"],
  "clarification_needed": false,
  "clarification_message": ""
}}

Rules:
1. "implicated_features" must only contain feature IDs from the available list: {available_specs}.
2. If the intent is too broad (e.g. asking to explain the whole system) or maps to more than 3 features, set "clarification_needed" to true and write a helpful message in "clarification_message" requesting the developer to narrow their focus.
3. Output ONLY valid JSON.
"""

    try:
        llm = OllamaLLM(model="llama3.2:3b", base_url="http://127.0.0.1:11434", timeout=30)
        response_text = llm.invoke(prompt)
        
        # Parse JSON
        # Clean response text in case LLM outputs markdown formatting
        cleaned_text = response_text.strip()
        if "```json" in cleaned_text:
            cleaned_text = cleaned_text.split("```json")[1].split("```")[0].strip()
        elif "```" in cleaned_text:
            cleaned_text = cleaned_text.split("```")[1].split("```")[0].strip()
            
        data = json.loads(cleaned_text)
        implicated = data.get("implicated_features", [])
        clarification_needed = data.get("clarification_needed", False)
        clarification_msg = data.get("clarification_message")
        
        # Enforce Top-K limit: if more than 3, trigger clarification
        if len(implicated) > 3 or clarification_needed:
            return {
                "implicated_features": [],
                "clarification_message": clarification_msg or "Your query is too broad. Please specify a narrower set of features (up to 3 specs)."
            }
            
        return {
            "implicated_features": implicated,
            "clarification_message": None
        }
    except Exception as e:
        print(f"Error calling Ollama router: {e}", file=sys.stderr)
        # Regex or simple string search fallback
        matches = [spec for spec in available_specs if spec.lower() in intent.lower()]
        return {
            "implicated_features": matches[:3],
            "clarification_message": None
        }

# Retrieve_Metadata Node
def retrieve_metadata_node(state: AgentState):
    clarification = state.get("clarification_message")
    if clarification:
        return {"metadata_docs": []}
        
    implicated = state.get("implicated_features", [])
    metadata_docs = []
    
    try:
        s3 = get_s3_client()
        for spec_id in implicated:
            key = f"metadata/{spec_id}.meta.json"
            try:
                obj = s3.get_object(Bucket='cckb-data', Key=key)
                meta_json = json.loads(obj['Body'].read().decode('utf-8'))
                metadata_docs.append(meta_json)
            except botocore.exceptions.ClientError as e:
                # KeyNotFound or other error - skip
                if e.response['Error']['Code'] == 'NoSuchKey':
                    print(f"Warning: Metadata map {key} not found on MinIO.", file=sys.stderr)
                else:
                    raise
    except Exception as e:
        print(f"Error fetching metadata from MinIO: {e}", file=sys.stderr)
        
    return {"metadata_docs": metadata_docs}

# Retrieve_Docs Node
def retrieve_docs_node(state: AgentState):
    clarification = state.get("clarification_message")
    if clarification:
        return {"feature_docs": [], "global_constitution": ""}
        
    implicated = state.get("implicated_features", [])
    feature_docs = []
    global_constitution = ""
    
    try:
        s3 = get_s3_client()
        # 1. Fetch spec markdown documents
        for spec_id in implicated:
            key = f"features/{spec_id}.md"
            try:
                obj = s3.get_object(Bucket='cckb-data', Key=key)
                doc_text = obj['Body'].read().decode('utf-8')
                feature_docs.append(doc_text)
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] == 'NoSuchKey':
                    print(f"Warning: Spec document {key} not found on MinIO.", file=sys.stderr)
                else:
                    raise
                    
        # 2. Fetch global hot_memory.md
        try:
            obj = s3.get_object(Bucket='cckb-data', Key='hot_memory.md')
            global_constitution = obj['Body'].read().decode('utf-8')
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                print("Warning: Global constitution hot_memory.md not found on MinIO.", file=sys.stderr)
            else:
                raise
    except Exception as e:
        print(f"Error fetching docs from MinIO: {e}", file=sys.stderr)
        
    return {
        "feature_docs": feature_docs,
        "global_constitution": global_constitution
    }

# Synthesize_Payload Node
def synthesize_payload_node(state: AgentState):
    clarification = state.get("clarification_message")
    intent = state.get("intent", "")
    
    if clarification:
        return {
            "final_payload": f"Clarification Needed:\n{clarification}",
            "history": [intent]
        }
        
    global_const = state.get("global_constitution", "")
    feature_docs = state.get("feature_docs", [])
    metadata_docs = state.get("metadata_docs", [])
    
    # Concatenate final optimized markdown payload
    payload_parts = []
    
    if global_const:
        payload_parts.append(f"# Global Constitution\n{global_const}")
        
    if feature_docs:
        payload_parts.append("# Feature Documentation")
        for doc in feature_docs:
            payload_parts.append(doc)
            
    if metadata_docs:
        payload_parts.append("# Codebase Mappings")
        for meta in metadata_docs:
            spec_id = meta.get("spec_id", "Unknown")
            payload_parts.append(f"## Feature ID: {spec_id}")
            payload_parts.append("Dependencies:")
            deps = meta.get("dependencies", [])
            if deps:
                for dep in deps:
                    payload_parts.append(f"- Type: {dep.get('type')}, Name: {dep.get('name')}, File: {dep.get('file')}")
            else:
                payload_parts.append("- None")
                
    final_payload = "\n\n".join(payload_parts)
    return {
        "final_payload": final_payload,
        "history": [intent]
    }

# 3. Graph Workflow Assembly
def create_graph_workflow():
    workflow = StateGraph(AgentState)
    
    workflow.add_node("analyze_intent", analyze_intent_node)
    workflow.add_node("retrieve_metadata", retrieve_metadata_node)
    workflow.add_node("retrieve_docs", retrieve_docs_node)
    workflow.add_node("synthesize_payload", synthesize_payload_node)
    
    # Conditional route: if clarification is needed, bypass retrieval nodes
    def router_condition(state: AgentState):
        if state.get("clarification_message"):
            return "synthesize_payload"
        return "retrieve_metadata"
        
    workflow.add_edge(START, "analyze_intent")
    workflow.add_conditional_edges(
        "analyze_intent",
        router_condition,
        {
            "synthesize_payload": "synthesize_payload",
            "retrieve_metadata": "retrieve_metadata"
        }
    )
    workflow.add_edge("retrieve_metadata", "retrieve_docs")
    workflow.add_edge("retrieve_docs", "synthesize_payload")
    workflow.add_edge("synthesize_payload", END)
    
    return workflow

def query_rag_engine(intent: str, thread_id: str = "default_thread") -> str:
    db_dir = ".cckb"
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "checkpoints.db")
    
    workflow = create_graph_workflow()
    
    # SqliteSaver checkpointer context
    with SqliteSaver.from_conn_string(db_path) as memory:
        app_graph = workflow.compile(checkpointer=memory)
        
        config = {"configurable": {"thread_id": thread_id}}
        initial_state = {
            "intent": intent,
            "implicated_features": [],
            "metadata_docs": [],
            "feature_docs": [],
            "global_constitution": "",
            "final_payload": "",
            "clarification_message": None,
            "history": []
        }
        
        result = app_graph.invoke(initial_state, config)
        return result.get("final_payload", "")

def main():
    parser = argparse.ArgumentParser(description="CCKB LangGraph RAG Engine")
    parser.add_argument("--query", type=str, required=True, help="Developer natural language intent string")
    parser.add_argument("--thread", type=str, default="user_thread_1", help="Conversation thread ID for multi-turn checkpoints")
    
    args = parser.parse_args()
    
    payload = query_rag_engine(args.query, args.thread)
    print("\n--- SYNTHESIZED PAYLOAD ---")
    print(payload)
    print("---------------------------\n")

if __name__ == "__main__":
    main()
