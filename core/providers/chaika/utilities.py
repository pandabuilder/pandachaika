import re
from datetime import datetime
from typing import Any

from core.base.types import GalleryData


class ChaikaGalleryData(GalleryData):

    def __init__(
        self,
        gid: str,
        provider: str,
        token: str | None = None,
        link: str | None = None,
        tags: list[str] | None = None,
        title: str | None = None,
        title_jpn: str | None = None,
        comment: str | None = None,
        category: str | None = None,
        posted: datetime | None = None,
        filesize: int | None = None,
        filecount: int | None = None,
        expunged: int | None = None,
        rating: str | None = None,
        fjord: bool | None = None,
        disowned: bool | None = None,
        hidden: bool | None = None,
        uploader: str | None = None,
        thumbnail_url: str | None = None,
        dl_type: str | None = None,
        public: bool | None = None,
        content: str | None = None,
        root: str | None = None,
        filename: str | None = None,
        queries: int | None = None,
        thumbnail: str | None = None,
        archives: list[dict[str, str]] | None = None,
        temp_archive: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            gid,
            token=token,
            link=link,
            tags=tags,
            provider=provider,
            title=title,
            title_jpn=title_jpn,
            comment=comment,
            category=category,
            posted=posted,
            filesize=filesize,
            filecount=filecount,
            expunged=expunged,
            disowned=disowned,
            rating=rating,
            fjord=fjord,
            hidden=hidden,
            uploader=uploader,
            thumbnail_url=thumbnail_url,
            dl_type=dl_type,
            public=public,
            content=content,
            root=root,
            filename=filename,
            queries=queries,
            thumbnail=thumbnail,
            **kwargs,
        )
        if archives is None:
            self.archives = []
        else:
            self.archives = archives
        self.temp_archive = temp_archive

    def to_gallery_data(self):

        attributes = vars(self)

        cleaned_attrs = {x: y for x, y in attributes.items() if x not in ["gid", "provider"]}

        return GalleryData(self.gid, self.provider, **cleaned_attrs)


def clean_title(title: str) -> str:
    # Remove parenthesis
    adjusted_title = re.sub(r"\s+\(.+?\)", r"", re.sub(r"\[.+?\]\s*", r"", title)).replace("_", "")
    # Remove non-words, non whitespace
    # adjusted_title = re.sub(r'[^\w\s]', r' ', adjusted_title)
    return adjusted_title
