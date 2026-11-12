"""Build a local, label-blind review form with each case's authorized corpus."""

import argparse
import hashlib
import json
from pathlib import Path

from app.api.documents import DocumentRepository
from eval.evaluate_answer_support import load_retrieval_inputs
from eval.evaluate_pg_hybrid_independent import load_documents, subjects
from eval.score_generated_answer_review import score


def prepare(report: dict, cards: list[dict], cases: dict, documents: dict) -> list[dict]:
    score(report, cards, documents)
    identities = subjects()
    repository = DocumentRepository(":memory:")
    try:
        ids = load_documents(repository, list(documents.values()), identities)
        keys = {document_id: key for key, document_id in ids.items()}
        result = []
        for card in cards:
            case = cases[card["case"]]
            subject = identities[case["role"]]
            area = case.get("area")
            visible = sorted(repository.list_visible(subject),
                             key=lambda record: keys[record.document_id])
            authorized = [
                {"title": record.title, "area": record.area,
                 "text": documents[keys[record.document_id]]["text"]}
                for record in visible if area is None or record.area == area
            ]
            result.append({**card, "review_context": {
                "role": case["role"], "area": area,
                "authorized_sources": authorized,
            }})
        return result
    finally:
        repository.close()


def validate_context(report: dict, cards: list[dict], cases: dict, documents: dict) -> None:
    """Reject changed authorization context in cards exported by the review UI."""
    present = ["review_context" in card for card in cards]
    if not any(present):
        return
    if not all(present):
        raise ValueError("REVIEW_CONTEXT_CHANGED")
    expected = prepare(report, cards, cases, documents)
    if any(card["review_context"] != original["review_context"]
           for card, original in zip(cards, expected, strict=True)):
        raise ValueError("REVIEW_CONTEXT_CHANGED")


def render_html(cards: list[dict]) -> str:
    payload = json.dumps(cards, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>生成答案人工复核</title><style>
body{font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;background:#f4f6f4;color:#1d3028;margin:0}
main{max-width:1040px;margin:0 auto;padding:32px 20px 80px}h1{font-size:28px;margin:0 0 8px}
.intro,.card{background:#fff;border:1px solid #dbe5dd;border-radius:16px;padding:20px;margin:16px 0;box-shadow:0 5px 22px #1739220b}
.muted{color:#587065}.case-id{font-size:13px;color:#638072}.question{font-size:19px;font-weight:650;margin:6px 0}
.answer{background:#edf5ef;padding:12px;border-radius:10px;white-space:pre-wrap}.source{background:#f6f8f6;padding:10px 12px;border-radius:9px;margin:8px 0}
.source p{margin:5px 0;white-space:pre-wrap}.claim{border-left:3px solid #69a27e;padding-left:10px;margin:12px 0}
select,textarea,button{font:inherit;border-radius:8px;border:1px solid #a9bfaf;padding:8px;background:#fff}
select{margin:5px 12px 5px 5px}textarea{display:block;width:100%;min-height:62px;box-sizing:border-box;margin-top:8px}
button{background:#2b6e48;color:#fff;border-color:#2b6e48;cursor:pointer;padding:10px 16px}
button:hover{background:#205939}.toolbar{display:flex;align-items:center;gap:15px;flex-wrap:wrap;margin:16px 0}
details{margin-top:13px}summary{cursor:pointer;font-weight:600}label{display:inline-block;margin:8px 0}
</style></head><body><main><h1>生成答案人工复核</h1>
<div class="intro">请由未参与编写这批资料、问题和模型提示词的评审者填写。只依据下方当前角色和资料范围内可见的原文判断。回答题分别判断答案是否准确回答问题、每句引用是否支持该句；拒答题判断已授权资料是否确实无法回答。若不能判断，保持“待评”并在备注中说明。此页面只在本地处理数据，点击导出后用评分脚本校验；不要把“待评”当作通过。</div>
<div class="toolbar"><button id="export">导出复核 JSON</button><span id="progress" class="muted"></span></div>
<div id="cards"></div></main><script>
const cards = __PAYLOAD__;
const root = document.getElementById('cards');
function element(tag, cls, value) { const e=document.createElement(tag); if(cls)e.className=cls; if(value!==undefined)e.textContent=value; return e; }
function select(card, key, labelText) {
  const label=element('label','',labelText); const control=element('select');
  [['','待评'],['true','是'],['false','否']].forEach(([value,text])=>{const option=element('option','',text);option.value=value;control.append(option)});
  control.value=card[key]===null?'':String(card[key]);
  control.addEventListener('change',()=>{card[key]=control.value===''?null:control.value==='true';progress()});
  label.append(control); return label;
}
cards.forEach((card,index)=>{
  const box=element('section','card');
  box.append(element('div','case-id',`${index+1}/${cards.length} · ${card.case} · 角色：${card.review_context.role} · 范围：${card.review_context.area||'全部授权资料'}`));
  box.append(element('div','question',card.question));
  const status=card.status==='answered'?'模型回答':card.status==='refused'?'模型拒答':card.status==='no_source'?'无检索来源，直接拒答':'模型调用错误';
  box.append(element('div','muted',status));
  if(card.status==='answered'){
    box.append(element('div','answer',card.answer));
    card.claims.forEach((claim,i)=>{
      const item=element('div','claim');
      item.append(element('div','',`第 ${i+1} 句：${claim.text}`));
      item.append(element('div','muted',`对应引句：${claim.evidence_span}`));box.append(item);
    });
    box.append(select(card,'human_answer_supported','答案准确且回答了问题：'));
    box.append(select(card,'human_citation_supported','每句均由对应引句支持：'));
  } else if(card.status==='refused'||card.status==='no_source') {
    box.append(select(card,'human_refusal_appropriate','拒答恰当：'));
  }
  if(card.source_text){
    const chosen=element('div','source');chosen.append(element('strong','',`被选来源：${card.source_title}`));
    chosen.append(element('p','',card.source_text));box.append(chosen);
  }
  const details=element('details');details.append(element('summary','',`查看该角色当前范围内的全部授权资料（${card.review_context.authorized_sources.length} 份）`));
  card.review_context.authorized_sources.forEach(source=>{
    const item=element('div','source');item.append(element('strong','',`${source.title} · ${source.area}`));
    item.append(element('p','',source.text));details.append(item);
  });box.append(details);
  const notes=element('textarea');notes.placeholder='备注：指出不支持的具体语句、缺失事实或无法判断的原因';
  notes.value=card.human_notes||'';notes.addEventListener('input',()=>{card.human_notes=notes.value});box.append(notes);
  root.append(box);
});
function progress(){const done=cards.filter(c=>c.status==='answered'?c.human_answer_supported!==null&&c.human_citation_supported!==null:(c.status==='refused'||c.status==='no_source')?c.human_refusal_appropriate!==null:false).length;document.getElementById('progress').textContent=`已评 ${done}/${cards.length}；错误项保持待评`;}
document.getElementById('export').addEventListener('click',()=>{
  const blob=new Blob([JSON.stringify(cards,null,2)+'\\n'],{type:'application/json'});
  const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='generated_answer_review_completed.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});progress();
</script></body></html>
""".replace("__PAYLOAD__", payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--retrieval-report", type=Path, required=True)
    parser.add_argument("--cards", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("OUTPUT_ALREADY_EXISTS")
    generated = json.loads(args.report.read_text(encoding="utf-8"))
    retrieval_bytes = args.retrieval_report.read_bytes()
    if hashlib.sha256(retrieval_bytes).hexdigest() != generated["retrieval_report_sha256"]:
        raise ValueError("RETRIEVAL_REPORT_MISMATCH")
    _, cases, documents = load_retrieval_inputs(args.retrieval_report)
    cards = json.loads(args.cards.read_text(encoding="utf-8"))
    args.output.write_text(render_html(prepare(generated, cards, cases, documents)),
                           encoding="utf-8")
    print(f"Prepared {len(cards)} review cards: {args.output}")


if __name__ == "__main__":
    main()
