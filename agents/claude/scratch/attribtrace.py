import sys, json, pickle, collections, time
sys.path.insert(0,'/home/claude/apt_viewer/Apt/beamng'); sys.argv=['x']
import engine_map as E
from capstone import x86
V=pickle.load(open('/tmp/V.pkl','rb'))
d=json.load(open('/tmp/dump/undercover_global_dump.json'))
nm=lambda h: V.get(h,'0x%08X'%h)
hx=lambda s:int(s,16)
kind={}
for c in d['classes']:
    ch=hx(c['hash']); kind[ch]=('class',nm(ch))
    for f in c['fields']: kind.setdefault(hx(f['hash']),('field',nm(hx(f['hash'])),nm(ch)))
    for x in c['collections']: kind.setdefault(hx(x['hash']),('coll',nm(hx(x['hash'])),nm(ch)))
exe=E.Exe('/tmp/stuff/nfs.exe')
cache={}
def scan(fva):
    if fva in cache: return cache[fva]
    hits=set(); calls=set()
    for i in exe.insns(fva,exe.func_end(fva)):
        for op in i.operands:
            if op.type==x86.X86_OP_IMM:
                v=op.imm&0xffffffff
                if i.mnemonic=='call': 
                    if exe.tv<=v<exe.tv+exe.tn: calls.add(v)
                elif v in kind and v>0xffff: hits.add(v)
    cache[fva]=(hits,calls); return cache[fva]
def trace(h,depth=3,limit=250):
    seen={h}; frontier=[h]; allhits=set()
    for _ in range(depth):
        nxt=[]
        for f in frontier:
            hits,calls=scan(f); allhits|=hits
            for c in calls:
                if c not in seen and len(seen)<limit: seen.add(c); nxt.append(c)
        frontier=nxt
    return allhits
rows=E.registrations(exe)
out={}
t=time.time()
for r in rows:
    if r['member'] and r['handler']:
        hs=trace(r['handler'])
        out.setdefault(r['member'],[]).append([kind[x] for x in sorted(hs)])
print(round(time.time()-t),'s;',sum(1 for v in out.values() if any(v)),'members reach attrib hashes of',len(out))
json.dump(out,open('/tmp/attrib_reach.json','w'))
