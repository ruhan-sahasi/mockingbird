# CloudFront is the one public address. Its default behavior serves the built web app from a
# private S3 bucket. The API paths go to the ALB, uncached, with every viewer header passed on.
#
# The web app routes by the URL hash (#/interview, #/report/<id>), so every page is /index.html
# and no error-page rewrite is needed. That matters: a custom error response would apply to the
# API paths too and turn the API's 404s into 200s.

resource "aws_s3_bucket" "web" {
  bucket_prefix = "${local.name}-web-"

  # The bucket holds only web/dist, which scripts/deploy.sh rebuilds, so `terraform destroy` may
  # empty it.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "web" {
  bucket = aws_s3_bucket.web.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "${local.name}-web"
  description                       = "CloudFront reads the private web bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Only this distribution may read the bucket, and only objects (no listing).
data "aws_iam_policy_document" "web_bucket" {
  statement {
    sid       = "CloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.web.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.main.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = data.aws_iam_policy_document.web_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.web]
}

data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

# Passes every viewer header, cookie and query string to the ALB, plus CloudFront's own headers.
# One of those is CloudFront-Viewer-Address, the visitor's real IP, which the per-visitor demo
# limit uses (limits.py). The rest are what WebSockets and the ws token need.
data "aws_cloudfront_origin_request_policy" "all_viewer_and_cloudfront" {
  name = "Managed-AllViewerAndCloudFrontHeaders-2022-06"
}

locals {
  web_origin_id = "web"
  api_origin_id = "api"
  api_paths     = ["/api/*", "/ws/*", "/vapi/*", "/healthz"]
}

resource "aws_cloudfront_distribution" "main" {
  enabled             = true
  comment             = local.name
  default_root_object = "index.html"

  # IPv4 only. The one-demo-per-IP limit means little over IPv6, where one person can hold a
  # whole block of addresses.
  is_ipv6_enabled = false

  origin {
    origin_id                = local.web_origin_id
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  origin {
    origin_id   = local.api_origin_id
    domain_name = aws_lb.main.dns_name

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = local.web_origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = data.aws_cloudfront_cache_policy.caching_optimized.id
    compress               = true
  }

  # https-only, not a redirect: the Vapi paths carry secrets and the socket URL carries a token,
  # so a client pointed at http:// should fail at once, not keep working through redirects.
  dynamic "ordered_cache_behavior" {
    for_each = local.api_paths

    content {
      path_pattern             = ordered_cache_behavior.value
      target_origin_id         = local.api_origin_id
      viewer_protocol_policy   = "https-only"
      allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
      cached_methods           = ["GET", "HEAD"]
      cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_and_cloudfront.id
      compress                 = false
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
