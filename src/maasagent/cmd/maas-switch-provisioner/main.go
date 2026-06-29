// Copyright (c) 2026 Canonical Ltd
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program.  If not, see <http://www.gnu.org/licenses/>.

// maas-switch-provisioner is executed by the ONIE provisioning wrapper script.
// It reads provisioning parameters from environment variables and orchestrates:
//  1. Updating switch status to DEPLOYING
//  2. Downloading and executing the NOS installer
//  3. Downloading and executing the operator provisioning script
//  4. Updating switch status to READY (or FAILED on error)
//
// All stdout/stderr from sub-processes is captured and uploaded to the MAAS
// v3 API with appropriate X-Log-Category and X-Exit-Code headers.
//
// Environment variables consumed:
//
//	MAAS_URL      - Region v3 API base URL, e.g. http://maas.local/MAAS/a/v3
//	SWITCH_UUID   - UUID of this switch
//	SWITCH_MAC    - Management MAC address
//	NOS_URL       - Full URL to the NOS installer binary on the rack
package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"
)

const (
	logCategoryNOSInstallation    = "NOS_INSTALLATION"
	logCategoryProvisioningScript = "PROVISIONING_SCRIPT"

	httpTimeout = 10 * time.Minute
	retryCount  = 3
	retryWait   = 2 * time.Second
)

var client = &http.Client{Timeout: httpTimeout}

func main() {
	maasURL := mustEnv("MAAS_URL")
	switchUUID := mustEnv("SWITCH_UUID")
	nosURL := os.Getenv("NOS_URL")

	// Derive endpoint URLs from MAAS_URL + SWITCH_UUID.
	statusURL := fmt.Sprintf("%s/switches/%s/status", maasURL, switchUUID)
	logURL := fmt.Sprintf("%s/switches/%s/logs", maasURL, switchUUID)
	provScriptURL := fmt.Sprintf("%s/switches/%s/provisioning-script", maasURL, switchUUID)

	log.Printf("maas-switch-provisioner starting for switch %s", switchUUID)

	if err := run(statusURL, logURL, provScriptURL, nosURL); err != nil {
		log.Printf("provisioning failed: %v", err)
		_ = updateStatus(statusURL, "FAILED")
		os.Exit(1)
	}
}

func mustEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		log.Fatalf("required environment variable %s is not set", key)
	}
	return v
}

func run(statusURL, logURL, provScriptURL, nosURL string) error {
	// 1. Mark as DEPLOYING.
	if err := updateStatus(statusURL, "DEPLOYING"); err != nil {
		log.Printf("warning: could not set status to DEPLOYING: %v", err)
	}

	// 2. NOS installer (only if a NOS_URL is provided).
	if nosURL != "" {
		log.Printf("downloading NOS installer from %s", nosURL)
		nosPath := os.TempDir() + "/nos-installer.bin"
		if err := downloadFile(nosURL, nosPath); err != nil {
			_ = uploadLog(logURL, logCategoryNOSInstallation, 1, fmt.Sprintf("failed to download NOS installer: %v\n", err))
			return fmt.Errorf("NOS download: %w", err)
		}
		if err := os.Chmod(nosPath, 0o755); err != nil {
			return fmt.Errorf("chmod NOS installer: %w", err)
		}

		log.Printf("executing NOS installer")
		nosOutput, nosExit := runCapture(nosPath)
		if err := uploadLog(logURL, logCategoryNOSInstallation, nosExit, nosOutput); err != nil {
			log.Printf("warning: could not upload NOS log: %v", err)
		}
		if nosExit != 0 {
			return fmt.Errorf("NOS installer exited with code %d", nosExit)
		}
	}

	// 3. Operator provisioning script.
	log.Printf("fetching provisioning script from %s", provScriptURL)
	scriptContent, err := fetchText(provScriptURL)
	if err != nil {
		// 404 means no script assigned — skip silently.
		if isNotFound(err) {
			log.Printf("no provisioning script assigned, skipping")
		} else {
			_ = uploadLog(logURL, logCategoryProvisioningScript, 1, fmt.Sprintf("failed to fetch provisioning script: %v\n", err))
			return fmt.Errorf("fetch provisioning script: %w", err)
		}
	} else {
		scriptPath := os.TempDir() + "/prov-script.sh"
		if err := os.WriteFile(scriptPath, []byte(scriptContent), 0o755); err != nil {
			return fmt.Errorf("write provisioning script: %w", err)
		}

		log.Printf("executing provisioning script")
		scriptOutput, scriptExit := runCapture(scriptPath)
		if err := uploadLog(logURL, logCategoryProvisioningScript, scriptExit, scriptOutput); err != nil {
			log.Printf("warning: could not upload provisioning script log: %v", err)
		}
		if scriptExit != 0 {
			return fmt.Errorf("provisioning script exited with code %d", scriptExit)
		}
	}

	// 4. Mark as READY.
	if err := updateStatus(statusURL, "READY"); err != nil {
		log.Printf("warning: could not set status to READY: %v", err)
	}
	log.Printf("provisioning complete")
	return nil
}

// updateStatus POSTs a plain-text status value to the MAAS status endpoint.
func updateStatus(statusURL, status string) error {
	return retry(func() error {
		resp, err := client.Post(
			statusURL,
			"text/plain",
			strings.NewReader(status),
		)
		if err != nil {
			return err
		}
		resp.Body.Close()
		if resp.StatusCode >= 400 {
			return fmt.Errorf("status update returned HTTP %d", resp.StatusCode)
		}
		return nil
	})
}

// uploadLog POSTs captured log output with categorical headers.
func uploadLog(logURL, category string, exitCode int, output string) error {
	return retry(func() error {
		req, err := http.NewRequestWithContext(
			context.Background(),
			http.MethodPost,
			logURL,
			strings.NewReader(output),
		)
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "text/plain")
		req.Header.Set("X-Log-Category", category)
		req.Header.Set("X-Exit-Code", fmt.Sprintf("%d", exitCode))

		resp, err := client.Do(req)
		if err != nil {
			return err
		}
		resp.Body.Close()
		if resp.StatusCode >= 400 {
			return fmt.Errorf("log upload returned HTTP %d", resp.StatusCode)
		}
		return nil
	})
}

// downloadFile downloads url and writes the body to dest.
func downloadFile(url, dest string) error {
	return retry(func() error {
		resp, err := client.Get(url)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		if resp.StatusCode == http.StatusNotFound {
			return &notFoundError{url: url}
		}
		if resp.StatusCode >= 400 {
			return fmt.Errorf("GET %s returned HTTP %d", url, resp.StatusCode)
		}
		f, err := os.Create(dest)
		if err != nil {
			return err
		}
		defer f.Close()
		_, err = io.Copy(f, resp.Body)
		return err
	})
}

// fetchText fetches a URL and returns the response body as a string.
func fetchText(url string) (string, error) {
	var body string
	err := retry(func() error {
		resp, err := client.Get(url)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		if resp.StatusCode == http.StatusNotFound {
			return &notFoundError{url: url}
		}
		if resp.StatusCode >= 400 {
			return fmt.Errorf("GET %s returned HTTP %d", url, resp.StatusCode)
		}
		b, err := io.ReadAll(resp.Body)
		if err != nil {
			return err
		}
		body = string(b)
		return nil
	})
	return body, err
}

// runCapture executes a command and returns its combined stdout+stderr and exit code.
func runCapture(path string, args ...string) (string, int) {
	cmd := exec.Command(path, args...)
	var buf bytes.Buffer
	cmd.Stdout = io.MultiWriter(os.Stdout, &buf)
	cmd.Stderr = io.MultiWriter(os.Stderr, &buf)
	err := cmd.Run()
	exitCode := 0
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
		} else {
			exitCode = 1
		}
	}
	return buf.String(), exitCode
}

// retry retries fn up to retryCount times with retryWait delay, skipping retry
// on notFoundError since those are definitive.
func retry(fn func() error) error {
	var lastErr error
	for i := 0; i < retryCount; i++ {
		lastErr = fn()
		if lastErr == nil {
			return nil
		}
		if isNotFound(lastErr) {
			return lastErr
		}
		if i < retryCount-1 {
			time.Sleep(retryWait)
		}
	}
	return lastErr
}

type notFoundError struct{ url string }

func (e *notFoundError) Error() string { return fmt.Sprintf("404 not found: %s", e.url) }

func isNotFound(err error) bool {
	_, ok := err.(*notFoundError)
	return ok
}
