# Native compose build chooses the current host architecture.
# Bake explicitly requests all three Linux variants, without publishing them.
variable "PLATFORMS" {
  default = ["linux/amd64", "linux/arm64", "linux/arm/v7"]
}

group "default" {
  targets = ["archive"]
}

target "archive" {
  context = "."
  dockerfile = "Dockerfile"
  target = "final"
  tags = ["ezviz-telegram-archive:local"]
  platforms = PLATFORMS
  output = ["type=oci,dest=dist/ezviz-telegram-archive-multiarch.tar"]
}

target "test" {
  inherits = ["archive"]
  target = "test"
  tags = ["ezviz-telegram-archive:test"]
  output = ["type=cacheonly"]
}

target "bot-api" {
  context = "."
  dockerfile = "Dockerfile.bot-api"
  tags = ["ezviz-telegram-bot-api:local"]
  platforms = PLATFORMS
  args = {
    BUILD_JOBS = "2"
  }
  output = ["type=oci,dest=dist/telegram-bot-api-multiarch.tar"]
}
