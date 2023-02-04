import json

from django.http import HttpResponse


class NonHtmlDebugToolbarMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        # One-time configuration and initialization.

    def __call__(self, request):
        response = self.get_response(request)

        if request.GET.get('debug'):
            if 'application/json' in response['Content-Type']:
                content = json.dumps(json.loads(response.content), sort_keys=True, indent=2)
                response = HttpResponse(u'<html><body><pre>{}</pre></body></html>'.format(content))
            elif 'application/rss+xml' in response['Content-Type']:
                response = HttpResponse(u'<html><body><pre>{}</pre></body></html>'.format(response.content))

        return response
