import os
import uuid
import chromadb
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
from openai import OpenAI
from pypdf import PdfReader
import io

# --- components loaded ONCE at startup ---
embedder = SentenceTransformer("all-MiniLM-L6-v2")
chroma = chromadb.PersistentClient(path="./chroma_db")
collection = chroma.get_or_create_collection("kb_docs")
ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434/v1")
llm = OpenAI(base_url=ollama_url, api_key="ollama")

def chunk_text(text: str, size: int = 500, overlap: int = 50) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += size - overlap
    return chunks

app = FastAPI()

class Query(BaseModel):
    question: str

def retrieve(question: str, k: int = 5):
    if collection.count() == 0:
        return []
    q_vec = embedder.encode([question]).tolist()
    res = collection.query(query_embeddings=q_vec, n_results=min(k, collection.count()))
    return res["documents"][0]

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    content = await file.read()
    reader = PdfReader(io.BytesIO(content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    chunks = [c for c in chunk_text(text) if c.strip()]
    if not chunks:
        raise HTTPException(
            status_code=400,
            detail="No extractable text in the PDF (it may be a scan / image-only file).",
        )
    vectors = embedder.encode(chunks).tolist()
    ids = [str(uuid.uuid4()) for _ in chunks]
    metas = [{"source": file.filename} for _ in chunks]
    collection.add(ids=ids, documents=chunks, embeddings=vectors, metadatas=metas)
    return {"uploaded": file.filename, "chunks": len(chunks)}

@app.get("/db")
def db_view():
    data = collection.get(include=["documents", "metadatas"])
    items = [
        {"id": id_, "source": (meta or {}).get("source"), "text": doc}
        for id_, doc, meta in zip(data["ids"], data["documents"], data["metadatas"])
    ]
    return {"count": collection.count(), "items": items}

@app.post("/reset")
def reset():
    global collection
    chroma.delete_collection("kb_docs")
    collection = chroma.get_or_create_collection("kb_docs")
    return {"status": "database cleared"}

@app.post("/ask")
def ask(q: Query):
    chunks = retrieve(q.question)
    if not chunks:
        raise HTTPException(status_code=400, detail="No documents loaded. Upload a PDF first.")
    context = "\n".join(chunks)
    prompt = (
        "Answer the question using ONLY the following context.\n\n"
        f"Context:\n{context}\n\nQuestion: {q.question}"
    )
    resp = llm.chat.completions.create(
        model="llama3.2",
        messages=[{"role": "user", "content": prompt}],
    )
    return {"answer": resp.choices[0].message.content, "context": chunks}

@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><body style="font-family:sans-serif;max-width:600px;margin:40px auto">
<h2>Mini RAG</h2>
<input type="file" id="pdf" accept=".pdf">
<button onclick="uploadPdf()">Upload PDF</button>
<button onclick="showDb()">Show DB</button>
<button onclick="resetDb()">Clear DB</button>
<br><br>
<input id="q" style="width:100%;padding:8px" placeholder="Ask a question...">
<button onclick="ask()">Send</button>
<pre id="out" style="white-space:pre-wrap;background:#f4f4f4;padding:12px"></pre>
<script>
async function uploadPdf(){
  const file=document.getElementById('pdf').files[0];
  if(!file){alert('Select a PDF first');return;}
  document.getElementById('out').textContent='Uploading...';
  try{
    const fd=new FormData();fd.append('file',file);
    const r=await fetch('/upload',{method:'POST',body:fd});
    const d=await r.json();
    if(!r.ok){document.getElementById('out').textContent='Error: '+(d.detail||r.status);return;}
    document.getElementById('out').textContent='Uploaded: '+d.uploaded+' ('+d.chunks+' chunks)';
  }catch(e){
    document.getElementById('out').textContent='Error: '+e.message;
  }
}
async function showDb(){
  const r=await fetch('/db');
  const d=await r.json();
  let out='VECTOR DB ('+d.count+' chunks)\\n\\n';
  d.items.forEach((it,i)=>{
    out+='['+i+'] source: '+it.source+'\\n'+it.text.slice(0,150)+'...\\n\\n';
  });
  document.getElementById('out').textContent=out;
}
async function resetDb(){
  const r=await fetch('/reset',{method:'POST'});
  const d=await r.json();
  document.getElementById('out').textContent=d.status;
}
async function ask(){
  document.getElementById('out').textContent='Thinking...';
  try{
    const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:document.getElementById('q').value})});
    const d=await r.json();
    if(!r.ok){document.getElementById('out').textContent='Error: '+(d.detail||r.status);return;}
    document.getElementById('out').textContent=d.answer+"\\n\\n--- context ---\\n"+d.context.join("\\n");
  }catch(e){
    document.getElementById('out').textContent='Error: '+e.message;
  }
}
</script></body>"""
