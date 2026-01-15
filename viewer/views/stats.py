import datetime

import pytz
from django.shortcuts import render
from django.http import JsonResponse
from django.db.models import Count, Q
from django.db.models.functions import TruncMonth

from viewer.forms import GallerySearchForm
from viewer.models import Tag, Gallery
from django.contrib.auth.decorators import login_required

@login_required
def stats_view(request):
    gallery_search_form = GallerySearchForm(form_name="filter-gallery-form")
    context = {
        'page_title': 'Stats & Visualization',
        'gallery_search_form': gallery_search_form,
    }
    return render(request, 'viewer/stats.html', context)

from viewer.views.head import filter_galleries_simple

DEFAULT_TIMELINE_ORDER = 'posted'
CUTOFF_DATE = datetime.datetime(1970, 1, 1, tzinfo=pytz.UTC)

@login_required
def stats_api(request):
    chart_type = request.GET.get('chart')
    chart_order = request.GET.get('timeline_order', DEFAULT_TIMELINE_ORDER)
    chart_tag_scope = request.GET.get('chart_tag_scope')

    if chart_order not in [DEFAULT_TIMELINE_ORDER, 'create_date']:
        chart_order = DEFAULT_TIMELINE_ORDER

    # Apply filters
    params = request.GET.dict()
    # Set default sort if not present, though filter_galleries_simple handles it
    if 'sort' not in params:
        params['sort'] = 'posted'
    if 'asc_desc' not in params:
        params['asc_desc'] = 'desc'
        
    # Ensure all expected keys are present for filter_galleries_simple
    # It expects a dictionary with keys matching the filter parameters
    # We can use a defaultdict or just ensure keys exist
    from collections import defaultdict
    safe_params = defaultdict(str)
    safe_params.update(params)
    
    filtered_galleries = filter_galleries_simple(safe_params)
    
    data = {}

    if chart_type == 'treemap' or chart_type is None:
        # 1. Tag Treemap Data
        # Filter tags based on filtered galleries AND scope if provided
        tag_filter = Q(gallery__in=filtered_galleries)
        if chart_tag_scope:
            tag_filter &= Q(scope=chart_tag_scope)

        top_tags = Tag.objects.filter(tag_filter).annotate(count=Count('gallery', filter=Q(gallery__in=filtered_galleries))).order_by('-count')[:100]
        treemap_data = []
        scope_dict = {}
        for tag in top_tags:
            if tag.count == 0:
                continue
            scope = tag.scope if tag.scope else "uncategorized"
            if scope not in scope_dict:
                scope_dict[scope] = []
            scope_dict[scope].append({
                "name": tag.name,
                "value": tag.count
            })
        
        for scope, children in scope_dict.items():
            treemap_data.append({
                "name": scope,
                "children": children
            })
        data["treemap"] = {
            "name": "Tags",
            "children": treemap_data
        }

    if chart_type == 'timeline' or chart_type is None:
        # 2. Upload Timeline
        # Filter archives based on filtered galleries
        timeline_data = []
        uploads = filtered_galleries.filter(posted__gt=CUTOFF_DATE).annotate(month=TruncMonth(chart_order)).values('month').annotate(count=Count('id')).order_by('month')
        
        for entry in uploads:
            if entry['month']:
                timeline_data.append({
                    "date": entry['month'].strftime("%Y-%m-%d"),
                    "count": entry['count']
                })
        data["timeline"] = timeline_data

    if chart_type == 'heatmap' or chart_type is None:
        # 3. Tag Co-occurrence Heatmap
        # Filter tags based on filtered galleries AND scope if provided
        tag_filter = Q(gallery__in=filtered_galleries)
        if chart_tag_scope:
            tag_filter &= Q(scope=chart_tag_scope)

        # Get top tags (limit to 40 for a readable matrix)
        top_tags = list(Tag.objects.filter(tag_filter).annotate(count=Count('gallery', filter=Q(gallery__in=filtered_galleries))).order_by('-count')[:40])
        
        heatmap_data = []
        tag_names = [t.name for t in top_tags]
        top_tag_ids = [t.id for t in top_tags]
        tag_names_map = {t.id: t.name for t in top_tags}
        # tag_order_map = {val: i for i, val in enumerate(top_tag_ids)}

        gallery_ids = [g.id for g in filtered_galleries]

        ThroughModel = Gallery.tags.through

        relationships = ThroughModel.objects.filter(
            tag_id__in=top_tag_ids,
            gallery_id__in=gallery_ids,
        ).values_list('gallery_id', 'tag_id')

        # 2. Group tags by gallery in memory
        # Format: { gallery_id: {tag_id_1, tag_id_2, ...} }
        gallery_tags = defaultdict(set)
        for gallery_id, tag_id in relationships:
            gallery_tags[gallery_id].add(tag_id)

        # 3. Calculate Co-occurrence
        # Initialize a 40x40 matrix (or a dictionary of counters)
        co_occurrence = defaultdict(int)

        # Iterate over the galleries and update counts for pairs
        for tags in gallery_tags.values():
            # Convert to list to iterate
            tags_list = list(tags)
            # tags_list = sorted(list(tags), key=lambda x: tag_order_map[x])
            # Double loop over the tags present in this specific gallery
            for i in range(len(tags_list)):
                for j in range(i, len(tags_list)):
                    t1 = tags_list[i]
                    t2 = tags_list[j]

                    # Create a sorted key so (A, B) is same as (B, A)
                    key = tuple(sorted((t1, t2)))
                    co_occurrence[key] += 1

        for (id1, id2), count in co_occurrence.items():
            heatmap_data.append({
                'x': tag_names_map[id1],
                'y': tag_names_map[id2],
                'value': count
            })

            if id1 != id2:
                heatmap_data.append({
                    'x': tag_names_map[id2],
                    'y': tag_names_map[id1],
                    'value': count
                })

        data["heatmap"] = {
            "tags": tag_names,
            "data": heatmap_data
        }

    if chart_type == 'sunburst' or chart_type is None:
        # 4. Sunburst Chart (Hierarchy)
        # Root -> Scope -> Tag
        
        tag_filter = Q(gallery__in=filtered_galleries)
        # Note: Sunburst usually shows ALL scopes, so we might ignore chart_tag_scope or use it to filter the "outer" ring?
        # Let's respect the scope filter if present, otherwise show all.
        if chart_tag_scope:
            tag_filter &= Q(scope=chart_tag_scope)

        # Get tags with counts
        tags = Tag.objects.filter(tag_filter).annotate(count=Count('gallery', filter=Q(gallery__in=filtered_galleries))).filter(count__gt=0)
        
        # Build hierarchy
        hierarchy = {"name": "All", "children": []}
        scopes = {} # scope_name -> list of children

        for tag in tags:
            scope_name = tag.scope if tag.scope else "uncategorized"
            if scope_name not in scopes:
                scopes[scope_name] = []
            
            scopes[scope_name].append({
                "name": tag.name,
                "value": tag.count
            })
        
        for scope_name, children in scopes.items():
            hierarchy["children"].append({
                "name": scope_name,
                "children": children
            })
            
        data["sunburst"] = hierarchy

    return JsonResponse(data)
