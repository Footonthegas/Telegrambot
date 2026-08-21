package main

import (
	"context"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/chromedp/chromedp"
)

type ResultHubHistory struct {
	CGPA           string   `json:"cgpa"`
	UniversityRank string   `json:"universityRank"`
	DeptRank       string   `json:"deptRank"`
	Credits        string   `json:"credits"`
	SGPA           []float64 `json:"sgpa"`
	Major          string   `json:"major"`
	Name           string   `json:"name"`
	URL            string   `json:"url"`
	ElapsedMs      int64    `json:"elapsed_ms"`
	Status         string   `json:"status"`
}

type ResultHubResult struct {
	Status    string            `json:"status"`
	ElapsedMs int64             `json:"elapsed_ms"`
	History   *ResultHubHistory `json:"history,omitempty"`
}

func scrapeResultHub(rollNumber string) (*ResultHubHistory, error) {
	year := 2028
	if len(rollNumber) >= 4 && strings.HasPrefix(rollNumber, "202") {
		if y, err := strconv.Atoi(rollNumber[:4]); err == nil {
			year = y + 4
		}
	}

	url := fmt.Sprintf("https://www.resulthubdtu.com/NSUT/StudentProfile/%d/%s", year, rollNumber)

	opts := append(chromedp.DefaultExecAllocatorOptions[:],
		chromedp.Flag("headless", "new"),
		chromedp.Flag("no-sandbox", true),
		chromedp.Flag("disable-setuid-sandbox", true),
		chromedp.Flag("disable-gpu", true),
		chromedp.Flag("disable-dev-shm-usage", true),
		chromedp.Flag("window-position", "-32000,-32000"),
		chromedp.Flag("disable-background-networking", true),
		chromedp.Flag("disable-sync", true),
		chromedp.Flag("disable-extensions", true),
		chromedp.Flag("no-first-run", true),
		chromedp.Flag("no-default-browser-check", true),
		chromedp.Flag("disable-translate", true),
		chromedp.UserAgent(`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36`),
	)

	allocCtx, cancel := chromedp.NewExecAllocator(context.Background(), opts...)
	defer cancel()

	ctx, cancel := chromedp.NewContext(allocCtx)
	defer cancel()

	ctx, cancel = context.WithTimeout(ctx, 45*time.Second)
	defer cancel()

	var renderedText string
	err := chromedp.Run(ctx,
		chromedp.EmulateViewport(800, 600),
		chromedp.Navigate(url),
		chromedp.Sleep(4*time.Second),
		chromedp.Evaluate(`document.body.innerText`, &renderedText),
	)
	if err != nil {
		return nil, fmt.Errorf("chromedp run failed: %w", err)
	}

	history := parseResultHubText(renderedText, url, rollNumber)
	return history, nil
}

func parseResultHubText(text, url, rollNumber string) *ResultHubHistory {
	history := &ResultHubHistory{
		URL: url,
	}

	cgpaMatch := regexp.MustCompile(`Cumulative CGPA[\s\n]*([\d\.]+)`).FindStringSubmatch(text)
	if len(cgpaMatch) > 1 {
		history.CGPA = cgpaMatch[1]
	}

	uniMatch := regexp.MustCompile(`University Rank[\s\n]*#?(\d+)`).FindStringSubmatch(text)
	if len(uniMatch) > 1 {
		history.UniversityRank = "#" + uniMatch[1]
	}

	deptMatch := regexp.MustCompile(`Dept\.?\s*Rank\s*#?(\d+)`).FindStringSubmatch(text)
	if len(deptMatch) > 1 {
		history.DeptRank = "#" + deptMatch[1]
	}

	creditsMatch := regexp.MustCompile(`Credits Completed[\s\n]*(\d+)`).FindStringSubmatch(text)
	if len(creditsMatch) > 1 {
		history.Credits = creditsMatch[1]
	}

	history.Major = "B.Tech"
	branchMatch := regexp.MustCompile(`B\.Tech\.\s*\n\s*([A-Z][A-Z\s]+)`).FindStringSubmatch(text)
	if len(branchMatch) > 1 {
		branch := strings.TrimSpace(branchMatch[1])
		switch {
		case strings.Contains(branch, "MECHANICAL"):
			history.Major = "Mechanical Engineering"
		case strings.Contains(branch, "COMPUTER") || strings.Contains(branch, "COE") || strings.Contains(branch, "CSE") || strings.Contains(branch, "CSA") || strings.Contains(branch, "AI") || strings.Contains(branch, "ML") || strings.Contains(branch, "DATA SCIENCE") || strings.Contains(branch, "SOFTWARE"):
			history.Major = "Computer Science"
		case strings.Contains(branch, "ELECTRONICS") || strings.Contains(branch, "ECE") || strings.Contains(branch, "EEE") || strings.Contains(branch, "E CE"):
			history.Major = "Electronics & Comm."
		case strings.Contains(branch, "CIVIL"):
			history.Major = "Civil Engineering"
		case strings.Contains(branch, "INFORMATION TECHNOLOGY") || strings.Contains(branch, "IT"):
			history.Major = "Information Technology"
		default:
			history.Major = branch
		}
	}

	history.Name = "Student"
	lines := strings.Split(text, "\n")
	for i, line := range lines {
		if strings.TrimSpace(line) == rollNumber && i > 0 {
			for j := i - 1; j >= 0 && j >= i-5; j-- {
				candidate := strings.TrimSpace(lines[j])
				if len(candidate) > 2 && regexp.MustCompile(`^[A-Z][A-Z\s]+$`).MatchString(candidate) && !strings.Contains(candidate, "STUDENT NOT FOUND") {
					history.Name = candidate
					break
				}
			}
			break
		}
	}

	history.SGPA = []float64{}
	for i, line := range lines {
		line = strings.TrimSpace(line)
		semMatch := regexp.MustCompile(`^Semester\s+(I|II|III|IV|V|VI|VII|VIII|\d+)$`).FindStringSubmatch(line)
		if len(semMatch) > 1 {
			for j := i + 1; j < len(lines) && j < i+6; j++ {
				nextLine := strings.TrimSpace(lines[j])
				if matched, _ := regexp.MatchString(`^\d+\s*cr$`, nextLine); matched {
					for k := j + 1; k < len(lines) && k < j+3; k++ {
						sgpaLine := strings.TrimSpace(lines[k])
						sgpaMatch := regexp.MustCompile(`^(\d+\.\d+)$`).FindStringSubmatch(sgpaLine)
						if len(sgpaMatch) > 1 {
							if val, err := strconv.ParseFloat(sgpaMatch[1], 64); err == nil {
								history.SGPA = append(history.SGPA, val)
							}
							break
						}
					}
					break
				}
			}
		}
	}

	if len(history.SGPA) == 0 {
		numbers := regexp.MustCompile(`\b\d+\.\d+\b`).FindAllString(text, -1)
		if len(numbers) > 0 {
			unique := make(map[float64]bool)
			var vals []float64
			for _, n := range numbers {
				if val, err := strconv.ParseFloat(n, 64); err == nil && val > 0 && val <= 10 {
					if !unique[val] {
						unique[val] = true
						vals = append(vals, val)
					}
				}
			}
			if len(vals) > 0 {
				sort.Float64s(vals)
				for i, j := 0, len(vals)-1; i < j; i, j = i+1, j-1 {
					vals[i], vals[j] = vals[j], vals[i]
				}
				history.SGPA = vals[:min(len(vals), 8)]
			}
		}
	}

	return history
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
