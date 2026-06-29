#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

from enum import Enum


class SwitchProvisioningStatus(str, Enum):
    NOT_PROVISIONED = "NOT_PROVISIONED"
    DEPLOYING = "DEPLOYING"
    READY = "READY"
    FAILED = "FAILED"


class SwitchLogCategory(str, Enum):
    WRAPPER = "WRAPPER"
    NOS_INSTALLATION = "NOS_INSTALLATION"
    PROVISIONING_SCRIPT = "PROVISIONING_SCRIPT"
