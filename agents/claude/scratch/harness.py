import sys, os, glob, collections
sys.path.insert(0,'/home/claude/apt_viewer/Apt'); sys.path.insert(0,'/home/claude/apt_viewer/common')
import apt_movie, apt_actions as AA, apt_decompile as D
def blocks(insns, out):
    out.append(insns)
    for i in insns:
        if i.body: blocks(i.body, out)
def allblocks():
    for p in sorted(glob.glob('/tmp/UI/UI/*.big')):
        try: mv=apt_movie.load(p); ss=AA.collect_streams(mv)
        except Exception as e: print('fail',p,e); continue
        for s in ss:
            ins = s['insns'] if isinstance(s,dict) else getattr(s,'insns',None)
            if ins is None: print(type(s), s if not isinstance(s,dict) else s.keys()); return
            bl=[]; blocks(ins,bl)
            for b in bl: yield os.path.basename(p), b
if __name__=='__main__':
    raw=collections.Counter(); n=0; fails=0
    for name,b in allblocks():
        n+=1
        try: lines,_=D.decompile(b)
        except Exception as e: fails+=1; print('EXC',name,e); continue
        for l in lines:
            if l.strip().startswith('/*'): raw[l.strip().split()[1]]+=1
    print(n,'blocks',fails,'fail'); print(raw.most_common())
