.DEFAULT_GOAL := all
.DELETE_ON_ERROR:

SHELL         := /bin/bash
.SHELLFLAGS   := -euo pipefail -c

GO      	 ?= go
BUILD_DIR  := build
VENDOR_DIR := vendor
BTF_PATH   := /sys/kernel/btf/vmlinux
BPFTOOL    ?= bpftool
GOFLAGS 	 ?= -mod=readonly
SWITCH_PROVISIONER_LDFLAGS ?= -s -w

# Explicitly set cache dirs to avoid situations where we can't mkdir under $HOME (e.g. Launchpad builds)
GOCACHE_DEFAULT    := $(HOME)/.cache/go-cache
GOMODCACHE_DEFAULT := $(HOME)/go/pkg/mod

export GOCACHE    = $(if $(wildcard $(HOME)/.cache),$(GOCACHE_DEFAULT),$(shell mktemp -d))
export GOMODCACHE = $(if $(wildcard $(HOME)/go),$(GOMODCACHE_DEFAULT),$(shell mktemp -d))

export MAKEDIR      = $(CURDIR)
export GOFLAGS

# Keep track of generated files required for build and test
generated := \

# Targets that must NOT trigger code generation
no_generate_targets := clean vendor format

.PHONY: all
all: build

DAEMON_ARTIFACTS := maas-agent maas-netmon
ARTIFACTS := $(DAEMON_ARTIFACTS)

# Architectures for the statically-linked switch provisioner binary.
# Key: path/Debian name → value: Go GOARCH name.
SWITCH_PROVISIONER_ARCHS := amd64 arm64 ppc64el
SWITCH_PROVISIONER_GOARCH_amd64  := amd64
SWITCH_PROVISIONER_GOARCH_arm64  := arm64
SWITCH_PROVISIONER_GOARCH_ppc64el := ppc64le

# Generate one build rule per architecture.
define switch_provisioner_rule
$(BUILD_DIR)/switch-provisioner/$(1)/maas-switch-provisioner: export CGO_ENABLED = 0
$(BUILD_DIR)/switch-provisioner/$(1)/maas-switch-provisioner: export GOOS = linux
$(BUILD_DIR)/switch-provisioner/$(1)/maas-switch-provisioner: export GOARCH = $(SWITCH_PROVISIONER_GOARCH_$(1))
$(BUILD_DIR)/switch-provisioner/$(1)/maas-switch-provisioner:
	@mkdir -p $$(@D)
	$$(GO) build -trimpath -ldflags="$(SWITCH_PROVISIONER_LDFLAGS)" -o $$@ ./cmd/maas-switch-provisioner
endef
$(foreach arch,$(SWITCH_PROVISIONER_ARCHS),$(eval $(call switch_provisioner_rule,$(arch))))

SWITCH_PROVISIONER_BINS := $(foreach arch,$(SWITCH_PROVISIONER_ARCHS),$(BUILD_DIR)/switch-provisioner/$(arch)/maas-switch-provisioner)

.PHONY: build
build: $(addprefix $(BUILD_DIR)/,$(ARTIFACTS)) $(SWITCH_PROVISIONER_BINS)

$(BUILD_DIR)/maas-agent: $(generated)
	$(GO) build $(GO_LDFLAGS) -o $@ ./cmd/maas-agent

$(BUILD_DIR)/maas-netmon: export CGO_ENABLED = 0
$(BUILD_DIR)/maas-netmon:
	$(GO) build -o $@ ./cmd/maas-netmon

.PHONY: install
install: build
	install -t $(DESTDIR)/usr/sbin -D $(addprefix $(BUILD_DIR)/,$(DAEMON_ARTIFACTS))
	$(foreach arch,$(SWITCH_PROVISIONER_ARCHS),\
		install -Dm 755 $(BUILD_DIR)/switch-provisioner/$(arch)/maas-switch-provisioner \
			$(DESTDIR)/usr/sbin/switch-provisioner/$(arch)/maas-switch-provisioner ;)

.PHONY: test
test: $(generated)
	$(GO) test ./...

.PHONY: test-race
test-race: export CGO_ENABLED = 1
test-race: $(generated)
	$(GO) test -race ./...

.PHONY: test-cover
test-cover: $(generated)
	$(GO) test -coverprofile=cover.out ./...

.PHONY: generate
generate: $(generated)
	$(GO) generate ./...

.PHONY: vendor
vendor:
	$(GO) mod tidy
	$(GO) mod verify
	$(GO) mod vendor

.PHONY: clean
clean:
	rm -rf $(BUILD_DIR) $(generated) $(addsuffix .d,$(generated)) cover.out

.PHONY: format
format:
	$(GO) fmt ./...

# Only include generated dependency tracking when not running no-generate targets
ifeq ($(filter $(no_generate_targets),$(MAKECMDGOALS)),)
endif
