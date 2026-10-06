"""学习通章节自动学习逻辑。"""

from __future__ import annotations

import re
import time
from typing import Any

from edge_workbench import TaskCancelled, now_text


MAX_CHAPTERS_PER_COURSE = 80
MAX_CHAPTER_ATTEMPTS = 3
VIDEO_POLL_SECONDS = 1.0


COURSE_CHAPTER_SCRIPT = r"""
els => els.map((el) => {
    const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
    const onclick = el.getAttribute("onclick") || "";
    const match = onclick.match(/toOld\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'/);
    const pending = el.querySelector(".catalog_jindu .catalog_points_yi, .catalog_jindu .catalog_tishi120");
    const completed = el.querySelector(".catalog_state.icon_yiwanc");
    const title = clean(el.getAttribute("title") || el.innerText || el.textContent);
    return {
        course_id: match ? match[1] : "",
        chapter_id: match ? match[2] : "",
        class_id: match ? match[3] : "",
        title,
        incomplete: Boolean(match && pending && !completed),
    };
})
"""


TASK_POINT_SCRIPT = r"""
els => els.map((el) => {
    const iframe = el.querySelector("iframe");
    const cls = String(el.className || "");
    return {
        finished: cls.includes("ans-job-finished"),
        src: iframe ? iframe.src : "",
        iframe_class: iframe ? String(iframe.className || "") : "",
        text: String(el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 240),
    };
})
"""


VIDEO_STATE_SCRIPT = r"""
() => {
    const video = document.querySelector("video, #video_html5_api");
    if (!video) return null;
    try { video.muted = true; video.defaultMuted = true; } catch (_error) {}
    const duration = Number(video.duration);
    const state = {
        ended: Boolean(video.ended),
        paused: Boolean(video.paused),
        duration: Number.isFinite(duration) ? duration : 0,
        currentTime: Number(video.currentTime) || 0,
    };
    if (!state.ended && state.paused) {
        try {
            const result = video.play();
            if (result && typeof result.catch === "function") result.catch(() => {});
        } catch (_error) {}
    }
    return state;
}
"""


DOCUMENT_SCROLL_SCRIPT = r"""
() => {
    const candidates = new Set([document.scrollingElement, document.documentElement, document.body]);
    document.querySelectorAll("#img, .imglook, #docContainer, .documentImg, [style*='overflow-y'], [style*='overflow-Y']").forEach((node) => candidates.add(node));
    let maxScrollTop = 0;
    for (const node of candidates) {
        if (!node) continue;
        const top = Math.max(Number(node.scrollHeight) || 0, Number(node.clientHeight) || 0);
        maxScrollTop = Math.max(maxScrollTop, top);
        try {
            node.scrollTop = top;
            if (typeof node.scrollTo === "function") node.scrollTo(0, top);
            node.dispatchEvent(new Event("scroll", { bubbles: true }));
        } catch (_error) {}
    }
    try { window.scrollTo(0, Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)); } catch (_error) {}
    return { maxScrollTop, scrollTop: document.scrollingElement?.scrollTop || 0 };
}
"""


BOOK_PAGE_SCRIPT = r"""
() => {
    const candidates = Array.from(document.querySelectorAll("a, button, input, [role='button']"));
    const next = candidates.find((node) => {
        const text = String(node.innerText || node.value || node.title || node.getAttribute("aria-label") || "");
        return /下一页|下页|next/i.test(text) && node.offsetParent !== null;
    });
    if (next) { next.click(); return true; }
    if (document.scrollingElement) document.scrollingElement.scrollTop = document.scrollingElement.scrollHeight;
    return false;
}
"""


class StudyAutomation:
    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.worker = owner.worker
        self.report_file = owner.runtime_dir / "study_report.json"

    def _emit(self, **payload: Any) -> None:
        payload.setdefault("mode", "study")
        self.owner._emit("chaoxing_run_status", **payload)

    def _log(self, message: str, level: str = "info") -> None:
        self.worker.log(message, level)

    def _check_cancelled(self) -> None:
        self.owner._check_cancelled()

    @staticmethod
    def _text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _needs_study(course: dict[str, Any]) -> bool:
        if not course.get("is_current"):
            return False
        checks: list[bool] = []
        for current_key, total_key in (("task_current", "task_total"), ("quiz_current", "quiz_total")):
            current = course.get(current_key)
            total = course.get(total_key)
            if isinstance(current, (int, float)) and isinstance(total, (int, float)):
                checks.append(float(current) >= float(total))
        progress = course.get("overall_progress")
        if isinstance(progress, (int, float)):
            checks.append(float(progress) >= 99.9)
        return (not checks) or not all(checks)

    def study_incomplete(self, **_payload: Any) -> None:
        report: dict[str, Any] = {"started_at": now_text(), "courses": [], "status": "running"}
        try:
            self._check_cancelled()
            self._emit(state="running", message="正在启动独立 Edge 并打开个人空间…", current=0, total=1)
            self.worker.start_browser()
            self.owner._restore_auth_state()
            page = self.owner._open_personal_space()
            if not self.owner._wait_for_login(page):
                return
            self._emit(state="running", message="正在读取课程列表…", current=0, total=1)
            courses = self.owner._extract_courses(page)
            # 先检查所有在读课程；章节目录会进一步确认哪些章节真正未完成。
            candidates = [course for course in courses if course.get("is_current") and course.get("course_url")]
            total = len(candidates)
            if total == 0:
                report.update({"status": "done", "finished_at": now_text()})
                self.owner._write_json(self.report_file, report)
                self._emit(state="done", message="没有检测到需要学习的未完成课程。", current=1, total=1)
                return
            report["course_total"] = total
            for index, course in enumerate(candidates, start=1):
                self._check_cancelled()
                title = self._text(course.get("title")) or "未知课程"
                self._emit(state="running", message=f"正在检查课程 {index}/{total}：{title}", current=index - 1, total=total)
                report["courses"].append(self._process_course(page, course, index, total))
            report.update({"status": "done", "finished_at": now_text()})
            self.owner._write_json(self.report_file, report)
            abnormal = sum(1 for item in report["courses"] if item.get("status") == "abnormal")
            suffix = f"，{abnormal} 门存在课程异常。" if abnormal else "。"
            self._emit(state="done", message=f"学习流程结束：检查 {total} 门课程{suffix}", current=total, total=total)
        except TaskCancelled:
            report.update({"status": "cancelled", "finished_at": now_text()})
            self.owner._write_json(self.report_file, report)
            self._emit(state="cancelled", message="学习流程已取消。", current=0, total=1)
            self._log("学习通自动学习已取消。", "warning")
        except Exception as exc:
            report.update({"status": "error", "error": str(exc), "finished_at": now_text()})
            self.owner._write_json(self.report_file, report)
            self._emit(state="error", message=f"自动学习失败：{exc}", current=0, total=1)
            self._log(f"学习通自动学习失败：{exc}", "error")

    def _process_course(self, page: Any, course: dict[str, Any], index: int, total: int) -> dict[str, Any]:
        title = self._text(course.get("title")) or "未知课程"
        result: dict[str, Any] = {"title": title, "course_id": self._text(course.get("course_id")), "chapters": [], "status": "done"}
        try:
            self._open_course_page(page, course.get("course_url", ""))
        except Exception as exc:
            result.update({"status": "error", "error": str(exc)})
            self._emit(state="warning", message=f"课程异常：{title} 无法打开课程页。", current=index, total=total)
            return result
        seen: set[str] = set()
        for _ in range(MAX_CHAPTERS_PER_COURSE):
            self._check_cancelled()
            incomplete = [ch for ch in self._course_incomplete_chapters(page) if ch.get("chapter_id") and ch["chapter_id"] not in seen]
            if not incomplete:
                break
            chapter = incomplete[0]
            chapter_id = self._text(chapter.get("chapter_id"))
            seen.add(chapter_id)
            chapter_title = self._text(chapter.get("title")) or chapter_id
            self._emit(state="running", message=f"{title}：正在学习 {chapter_title}", current=index, total=total)
            try:
                self._open_chapter(page, chapter)
                ok = self._process_chapter(page, chapter_title, index, total)
                result["chapters"].append({"chapter_id": chapter_id, "title": chapter_title, "status": "done" if ok else "abnormal"})
                if not ok:
                    result["status"] = "abnormal"
            except Exception as exc:
                result["status"] = "abnormal"
                result["chapters"].append({"chapter_id": chapter_id, "title": chapter_title, "status": "error", "error": str(exc)})
                self._emit(state="warning", message=f"课程异常：{chapter_title} 处理失败（{exc}），继续检查后续章节。", current=index, total=total)
            self._return_to_course(page, course.get("course_url", ""))
        else:
            result.update({"status": "abnormal", "error": "达到单门课程最大章节处理数量。"})
        return result

    def _open_course_page(self, page: Any, course_url: str) -> None:
        if not course_url:
            raise RuntimeError("课程入口为空")
        self.worker._goto_page(page, course_url, wait_network_idle=True)
        self._wait_course_page(page)

    def _wait_course_page(self, page: Any) -> None:
        try:
            page.wait_for_selector("#frame_content-zj", timeout=30_000)
        except Exception:
            if page.locator("#iframe").count():
                self._return_to_course(page, page.url)
                return
            raise

    def _course_incomplete_chapters(self, page: Any) -> list[dict[str, Any]]:
        self._wait_course_page(page)
        frame = page.frame_locator("#frame_content-zj")
        try:
            frame.locator(".chapter_item").first.wait_for(state="attached", timeout=20_000)
            items = frame.locator(".chapter_item").evaluate_all(COURSE_CHAPTER_SCRIPT)
        except Exception:
            return []
        return [item for item in (items or []) if isinstance(item, dict) and item.get("incomplete") and item.get("chapter_id")]

    def _open_chapter(self, page: Any, chapter: dict[str, Any]) -> None:
        chapter_id = self._text(chapter.get("chapter_id"))
        frame = page.frame_locator("#frame_content-zj")
        locator = frame.locator(f"#cur{chapter_id}")
        if locator.count() == 0:
            locator = frame.get_by_text(self._text(chapter.get("title")), exact=False).first
        locator.scroll_into_view_if_needed(timeout=10_000)
        locator.click(timeout=10_000)
        try:
            page.wait_for_url(re.compile(r".*/mycourse/studentstudy.*"), timeout=45_000)
        except Exception:
            if page.locator("#iframe").count() == 0:
                raise
        self._wait_study_page(page)

    def _wait_study_page(self, page: Any) -> None:
        page.wait_for_selector("#iframe", timeout=30_000)
        page.wait_for_selector("#prevNextFocusNext", state="attached", timeout=30_000)
        try:
            page.frame_locator("#iframe").locator("body").wait_for(state="attached", timeout=20_000)
        except Exception:
            pass
        page.wait_for_timeout(1_000)

    def _task_points(self, page: Any) -> list[dict[str, Any]]:
        try:
            values = page.frame_locator("#iframe").locator(".ans-attach-ct").evaluate_all(TASK_POINT_SCRIPT)
        except Exception:
            return []
        return [item for item in (values or []) if isinstance(item, dict)]


    def _chapter_complete(self, page: Any) -> bool:
        if self._visible_popup(page) is not None:
            return False
        tasks = self._task_points(page)
        if tasks:
            return all(bool(item.get("finished")) for item in tasks)
        try:
            body = page.frame_locator("#iframe").locator("body").inner_text(timeout=3_000)
        except Exception:
            body = ""
        return "任务点已完成" in body and "任务点未完成" not in body

    def _process_chapter(self, page: Any, chapter_title: str, course_index: int, total: int) -> bool:
        for attempt in range(1, MAX_CHAPTER_ATTEMPTS + 1):
            self._check_cancelled()
            self._emit(state="running", message=f"{chapter_title}：正在处理任务点（第 {attempt}/{MAX_CHAPTER_ATTEMPTS} 次检查）…", current=course_index, total=total)
            self._complete_task_points(page, chapter_title, course_index, total)
            page.wait_for_timeout(1_500)
            if self._chapter_complete(page):
                return True
            self._emit(state="warning", message=f"{chapter_title}：仍有任务点未完成，准备再次检查。", current=course_index, total=total)
            page.wait_for_timeout(1_000)
        self._emit(state="warning", message=f"课程异常：{chapter_title} 连续 {MAX_CHAPTER_ATTEMPTS} 次检查仍有任务点未完成，已点击下一节跳过。", current=course_index, total=total)
        self._skip_unfinished_chapter(page)
        return False

    def _complete_task_points(self, page: Any, chapter_title: str, course_index: int, total: int) -> None:
        tasks = self._task_points(page)
        unfinished = [item for item in tasks if not item.get("finished")]
        if not unfinished:
            return
        sources = [self._text(item.get("src")) for item in unfinished]
        video_sources = [source for source in sources if "video" in source]
        document_sources = [source for source in sources if any(key in source for key in ("pdf", "zt/", "insertdoc"))]
        book_sources = [source for source in sources if any(key in source for key in ("innerbook", "book"))]
        if video_sources:
            self._complete_video_tasks(page, video_sources, chapter_title, course_index, total)
        if document_sources:
            self._scroll_document_tasks(page, document_sources, chapter_title, course_index, total)
        if book_sources:
            self._advance_book_tasks(page, book_sources, chapter_title, course_index, total)
        if not (video_sources or document_sources or book_sources):
            page.wait_for_timeout(3_000)

    def _matching_frames(self, page: Any, sources: list[str]) -> list[Any]:
        matched: list[Any] = []
        for frame in page.frames:
            url = self._text(getattr(frame, "url", ""))
            if any(source and (source == url or source in url or url in source) for source in sources):
                matched.append(frame)
        return matched

    def _complete_video_tasks(self, page: Any, sources: list[str], chapter_title: str, course_index: int, total: int) -> None:
        for frame in self._matching_frames(page, sources):
            self._check_cancelled()
            try:
                initial = frame.evaluate(VIDEO_STATE_SCRIPT)
            except Exception:
                continue
            if not isinstance(initial, dict):
                continue
            duration = float(initial.get("duration") or 0)
            if bool(initial.get("ended")) or (duration > 0 and float(initial.get("currentTime") or 0) >= duration * 0.95):
                continue
            self._emit(state="running", message=f"{chapter_title}：正在播放视频…", current=course_index, total=total)
            deadline = time.monotonic() + (max(600.0, min(21_600.0, duration * 1.6 + 180.0)) if duration > 0 else 21_600.0)
            last_report = 0.0
            while time.monotonic() < deadline:
                self._check_cancelled()
                try:
                    state = frame.evaluate(VIDEO_STATE_SCRIPT)
                except Exception:
                    break
                if not isinstance(state, dict):
                    break
                current = float(state.get("currentTime") or 0)
                total_seconds = float(state.get("duration") or duration or 0)
                if bool(state.get("ended")) or (total_seconds > 0 and current >= total_seconds * 0.95):
                    break
                now = time.monotonic()
                if now - last_report >= 15.0:
                    last_report = now
                    if total_seconds > 0:
                        percent = int(max(0, min(100, current / total_seconds * 100)))
                        self._emit(state="running", message=f"{chapter_title}：视频播放中 {percent}%", current=percent, total=100)
                    else:
                        self._emit(state="running", message=f"{chapter_title}：视频播放中…", current=course_index, total=total)
                page.wait_for_timeout(int(VIDEO_POLL_SECONDS * 1000))
            else:
                self._log(f"{chapter_title}：视频播放等待超时，继续检查任务点。", "warning")

    def _scroll_document_tasks(self, page: Any, sources: list[str], chapter_title: str, course_index: int, total: int) -> None:
        frames = self._matching_frames(page, sources)
        extra = [
            frame for frame in page.frames
            if any(token in self._text(getattr(frame, "url", "")) for token in ("pan-yz.chaoxing.com/screen", "readsvr-fanya.sslibrary.com", "resapi.chaoxing.com"))
        ]
        for frame in extra:
            if frame not in frames:
                frames.append(frame)
        if not frames:
            return
        for round_index in range(1, 4):
            self._check_cancelled()
            for frame in frames:
                try:
                    frame.evaluate(DOCUMENT_SCROLL_SCRIPT)
                except Exception:
                    continue
            self._emit(state="running", message=f"{chapter_title}：正在滚动 PPT/PDF（{round_index}/3）…", current=course_index, total=total)
            page.wait_for_timeout(2_000)

    def _advance_book_tasks(self, page: Any, sources: list[str], chapter_title: str, course_index: int, total: int) -> None:
        frames = self._matching_frames(page, sources)
        extra = [
            frame for frame in page.frames
            if "readsvr-fanya.sslibrary.com" in self._text(getattr(frame, "url", ""))
        ]
        for frame in extra:
            if frame not in frames:
                frames.append(frame)
        for round_index in range(1, 4):
            self._check_cancelled()
            clicked = False
            for frame in frames:
                try:
                    clicked = bool(frame.evaluate(BOOK_PAGE_SCRIPT)) or clicked
                except Exception:
                    continue
            self._emit(state="running", message=f"{chapter_title}：正在处理电子书任务（{round_index}/3）…", current=course_index, total=total)
            page.wait_for_timeout(2_000)
            if not clicked:
                break

    def _visible_popup(self, page: Any) -> Any | None:
        popups = page.locator("div.maskDiv.jobFinishTip")
        try:
            count = popups.count()
        except Exception:
            return None
        for index in range(count):
            popup = popups.nth(index)
            try:
                if popup.is_visible():
                    return popup
            except Exception:
                continue
        return None

    def _click_page_next(self, page: Any) -> bool:
        button = page.locator("#prevNextFocusNext")
        try:
            if button.count() and button.first.is_visible() and button.first.is_enabled():
                button.first.click()
                return True
        except Exception:
            pass
        return False

    def _skip_unfinished_chapter(self, page: Any) -> None:
        popup = self._visible_popup(page)
        if popup is not None:
            candidates = popup.locator("a, button, [role='button']")
            try:
                for index in range(candidates.count()):
                    candidate = candidates.nth(index)
                    if "下一节" in self._text(candidate.inner_text(timeout=1_000)) and candidate.is_visible():
                        candidate.click()
                        page.wait_for_timeout(1_800)
                        return
            except Exception:
                pass
        self._click_page_next(page)
        page.wait_for_timeout(1_800)

    def _return_to_course(self, page: Any, course_url: str) -> None:
        try:
            back = page.locator("#contentFocus")
            if back.count() and back.first.is_visible():
                back.first.click()
                try:
                    page.wait_for_url(re.compile(r".*/mycourse/stu.*"), timeout=30_000)
                except Exception:
                    pass
            else:
                self.worker._goto_page(page, course_url, wait_network_idle=True)
        except Exception:
            self.worker._goto_page(page, course_url, wait_network_idle=True)
        self._wait_course_page(page)
