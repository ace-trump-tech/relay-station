"""Canvas LMS adapter using a user-authorized access token and GET requests only."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from .connectors import plain_html
from .organizer import iso


def canvas_origin(value):
    value=value.rstrip("/")
    parsed=urlsplit(value)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("填写学校 Canvas 的 HTTPS 站点根地址，不包含路径或登录参数")
    return value


class Canvas:
    def __init__(self,config):
        self.config=config
        self.origin=canvas_origin(config["base_url"])
        self.headers={"Authorization":"Bearer "+config["access_token"]}
        self.truncated=False

    async def pages(self,client,path,params=None,max_pages=3):
        url=self.origin+path
        output=[]
        for page in range(max_pages):
            parsed=urlsplit(url)
            if (parsed.scheme,parsed.netloc)!=(urlsplit(self.origin).scheme,urlsplit(self.origin).netloc) or not parsed.path.startswith("/api/v1/"):
                raise ValueError("Refusing to forward a Canvas token to a foreign pagination URL")
            response=await client.get(url,params=params if page==0 else None,headers=self.headers)
            response.raise_for_status()
            data=response.json()
            if not isinstance(data,list):
                raise ValueError("Invalid Canvas response")
            output.extend(data)
            url=response.links.get("next",{}).get("url")
            if not url:
                return output
        self.truncated=True
        return output

    async def verify(self):
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            response=await client.get(self.origin+"/api/v1/users/self/profile",headers=self.headers)
            response.raise_for_status()
            identity=response.json()
            courses=await self.pages(client,"/api/v1/courses",{"enrollment_state":"active","per_page":50})
        return {"name":str(identity.get("name","Canvas 用户"))[:120],
                "courses":[{"id":c["id"],"name":str(c.get("name",c["id"]))[:200]} for c in courses if isinstance(c.get("id"),int)]}

    async def sync(self):
        assignments,blocks=[],[]
        current=datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            courses=await self.pages(client,"/api/v1/courses",{"enrollment_state":"active","per_page":50})
            courses=[c for c in courses if isinstance(c.get("id"),int) and (not self.config.get("course_ids") or c["id"] in self.config["course_ids"])]
            if len(courses)>25:
                self.truncated=True
            courses=courses[:25]
            if self.config.get("sync_assignments",True):
                for course in courses:
                    rows=await self.pages(client,f"/api/v1/courses/{course['id']}/assignments",
                        {"per_page":50,"include[]":"submission","override_assignment_dates":"true","order_by":"due_at"})
                    for row in rows:
                        if not isinstance(row.get("id"),int) or row.get("published") is False:
                            continue
                        due=iso(row.get("due_at"))
                        if due and datetime.fromisoformat(due)<current-timedelta(days=30):
                            continue
                        assignments.append({"external_id":f"{self.origin}:{course['id']}:{row['id']}",
                            "title":str(row.get("name","未命名作业"))[:200],"course":str(course.get("name","课程"))[:200],
                            "notes":plain_html(row.get("description") or ""),"due_at":due,
                            "source_url":self.origin+f"/courses/{course['id']}/assignments/{row['id']}",
                            "submission_status":(row.get("submission") or {}).get("workflow_state","unsubmitted")})
            if self.config.get("sync_calendar",False):
                for start in range(0,len(courses),10):
                    params=[("type","event"),("per_page","100"),("start_date",current.date().isoformat()),
                            ("end_date",(current+timedelta(days=14)).date().isoformat())]
                    params += [("context_codes[]",f"course_{c['id']}") for c in courses[start:start+10]]
                    for event in await self.pages(client,"/api/v1/calendar_events",params):
                        if event.get("all_day") or not event.get("start_at") or not event.get("end_at") or event.get("workflow_state")=="deleted":
                            continue
                        a,b=iso(event["start_at"]),iso(event["end_at"])
                        if a>=b:
                            continue
                        blocks.append({"external_key":f"canvas:{self.origin}:{event['id']}","title":str(event.get("title","课程安排"))[:200],
                            "start":a,"end":b,"source":"canvas","location":str(event.get("location_name", ""))[:200]})
        return {"assignments":assignments,"blocks":blocks,"truncated":self.truncated}
