from datetime import datetime
from typing import Optional, Any

from core.base.types import GalleryData


class ChaikaGalleryData(GalleryData):

    def __init__(self, gid: str, provider: str, token: Optional[str] = None, link: Optional[str] = None,
                 tags: Optional[list[str]] = None, title: Optional[str] = None,
                 title_jpn: Optional[str] = None, comment: Optional[str] = None, category: Optional[str] = None,
                 posted: Optional[datetime] = None, filesize: Optional[int] = None, filecount: Optional[int] = None,
                 expunged: Optional[int] = None, rating: Optional[str] = None, fjord: Optional[bool] = None,
                 hidden: Optional[bool] = None, uploader: Optional[str] = None, thumbnail_url: Optional[str] = None,
                 dl_type: Optional[str] = None, public: Optional[bool] = None, content: Optional[str] = None,
                 archiver_key: Optional[str] = None, root: Optional[str] = None, filename: Optional[str] = None,
                 queries: Optional[int] = None, thumbnail: Optional[str] = None,
                 archives: Optional[list[dict[str, str]]] = None, temp_archive: dict[str, str] = None,
                 **kwargs: Any
                 ) -> None:
        super().__init__(
            gid, token=token, link=link, tags=tags, provider=provider, title=title, title_jpn=title_jpn,
            comment=comment, category=category, posted=posted, filesize=filesize,
            filecount=filecount, expunged=expunged, rating=rating, fjord=fjord, hidden=hidden,
            uploader=uploader, thumbnail_url=thumbnail_url, dl_type=dl_type, public=public, content=content,
            archiver_key=archiver_key, root=root, filename=filename, queries=queries, thumbnail=thumbnail,
            **kwargs
        )
        if archives is None:
            self.archives = []
        else:
            self.archives = archives
        self.temp_archive = temp_archive

    def to_gallery_data(self):

        attributes = vars(self)

        cleaned_attrs = {x: y for x, y in attributes.items() if x not in ['gid', 'provider']}

        return GalleryData(self.gid, self.provider, **cleaned_attrs)
