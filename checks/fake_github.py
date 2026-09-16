"""An in-memory GitHub Git Data API, so the sync code can be tested offline.

Mirrors only what github_sync.py calls: branch ref -> commit -> tree -> blobs,
blob/tree/commit creation, and the ref PATCH that either fast-forwards or
returns 422 the way GitHub does. `use(fake)` swaps it into github_sync in place
of `requests` and makes the retry sleeps instant.
"""
import base64
import itertools
import json
import types

import github_sync as gs


class FakeGitHub:
    def __init__(self, files):
        self.blobs={}; self.trees={}; self.commits={}; self.n=itertools.count()
        tree=self._tree({k:self._blob(json.dumps(v,indent=2).encode()) for k,v in files.items()})
        self.head=self._commit(tree,[]); self.on_patch=None; self.patches=0; self.commits_made=0
    def _id(self,p): return f"{p}{next(self.n)}"
    def _blob(self,b): i=self._id("b"); self.blobs[i]=b; return i
    def _tree(self,e): i=self._id("t"); self.trees[i]=dict(e); return i
    def _commit(self,tree,parents): i=self._id("c"); self.commits[i]={"tree":tree,"parents":parents}; return i
    def commit_file(self,name,data):
        t=dict(self.trees[self.commits[self.head]["tree"]]); t[name]=self._blob(json.dumps(data,indent=2).encode())
        self.head=self._commit(self._tree(t),[self.head])
    def file(self,name): return json.loads(self.blobs[self.trees[self.commits[self.head]["tree"]][name]])
    def resp(self,code,js=None,content=b""): return types.SimpleNamespace(status_code=code,json=lambda: js,content=content,text=json.dumps(js))
    def get(self,url,headers=None,timeout=None):
        p=url.split("/repos/o/r/")[1]
        if p.startswith("git/ref/heads/"): return self.resp(200,{"object":{"sha":self.head}})
        if p.startswith("git/commits/"): return self.resp(200,{"tree":{"sha":self.commits[p.split("/")[-1]]["tree"]}})
        if p.startswith("git/trees/"):
            k=p.split("/")[-1]; tid=self.commits[self.head]["tree"] if k=="main" else k
            return self.resp(200,{"tree":[{"path":a,"sha":b} for a,b in self.trees[tid].items()]})
        if p.startswith("git/blobs/"): return self.resp(200,None,self.blobs[p.split("/")[-1]])
        raise AssertionError(url)
    def post(self,url,headers=None,json=None,timeout=None):
        p=url.split("/repos/o/r/")[1]
        if p=="git/blobs": return self.resp(201,{"sha":self._blob(base64.b64decode(json["content"]))})
        if p=="git/trees":
            t=dict(self.trees[json["base_tree"]]); t.update({e["path"]:e["sha"] for e in json["tree"]}); return self.resp(201,{"sha":self._tree(t)})
        if p=="git/commits": self.commits_made+=1; return self.resp(201,{"sha":self._commit(json["tree"],json["parents"])})
        raise AssertionError(url)
    def patch(self,url,headers=None,json=None,timeout=None):
        self.patches+=1
        if self.on_patch: self.on_patch(); self.on_patch=None
        if self.commits[json["sha"]]["parents"]!=[self.head]: return self.resp(422,{"message":"Update is not a fast forward"})
        self.head=json["sha"]; return self.resp(200,{})

def use(fk):
    gs.requests=types.SimpleNamespace(get=fk.get,post=fk.post,patch=fk.patch,RequestException=Exception); gs.time.sleep=lambda s: None
