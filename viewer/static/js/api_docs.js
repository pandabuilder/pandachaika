// Interactive API docs (/api/v1/docs/): Swagger UI on the generated OpenAPI document.
(function () {
  "use strict";
  const container = document.getElementById("swagger-ui");
  if (!container || typeof SwaggerUIBundle === "undefined") {
    return;
  }
  SwaggerUIBundle({
    url: container.dataset.openapiUrl,
    domNode: container,
    deepLinking: true,
    docExpansion: "none",
    filter: true,
    persistAuthorization: true,
    tryItOutEnabled: false,
    // Session-authenticated try-it-out requests send the session cookie.
    withCredentials: true,
  });
})();
