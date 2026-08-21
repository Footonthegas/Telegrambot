package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/PuerkitoBio/goquery"
)

const (
	imsBase        = "https://www.imsnsit.org/imsnsit/"
	imsLoginURL    = "https://www.imsnsit.org/imsnsit/student.htm"
	userAgent      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/146.0.0.0 Safari/537.36"
	httpTimeout    = 15 * time.Second
	connectTimeout = 10 * time.Second
	maxRetries     = 5
	cookieFile     = "session_cookies.json"
)

type ScrapeResult struct {
	Data         map[string]map[string]int
	Timeline     map[string][]map[string]string
	SubjectNames map[string]string
	Status       string
	ElapsedMs    int64
}

func saveCookies(client *http.Client) error {
	jar := client.Jar
	if jar == nil {
		return nil
	}
	cookies := jar.Cookies(&url.URL{Scheme: "https", Host: "www.imsnsit.org", Path: "/"})
	if len(cookies) == 0 {
		return nil
	}
	type cookieDTO struct {
		Name     string
		Value    string
		Path     string
		Domain   string
		Expires  int64
		Secure   bool
		HttpOnly bool
	}
	var dtos []cookieDTO
	for _, c := range cookies {
		dto := cookieDTO{
			Name:     c.Name,
			Value:    c.Value,
			Path:     c.Path,
			Domain:   c.Domain,
			Secure:   c.Secure,
			HttpOnly: c.HttpOnly,
		}
		if !c.Expires.IsZero() {
			dto.Expires = c.Expires.Unix()
		}
		dtos = append(dtos, dto)
	}
	data, err := json.Marshal(dtos)
	if err != nil {
		return err
	}
	return os.WriteFile(cookieFile, data, 0600)
}

func loadCookies(client *http.Client) error {
	data, err := os.ReadFile(cookieFile)
	if err != nil {
		return err
	}
	type cookieDTO struct {
		Name     string
		Value    string
		Path     string
		Domain   string
		Expires  int64
		Secure   bool
		HttpOnly bool
	}
	var dtos []cookieDTO
	if err := json.Unmarshal(data, &dtos); err != nil {
		return err
	}
	jar := client.Jar
	if jar == nil {
		var err error
		jar, err = cookiejar.New(nil)
		if err != nil {
			return err
		}
		client.Jar = jar
	}
	for _, dto := range dtos {
		c := &http.Cookie{
			Name:     dto.Name,
			Value:    dto.Value,
			Path:     dto.Path,
			Domain:   dto.Domain,
			Secure:   dto.Secure,
			HttpOnly: dto.HttpOnly,
		}
		if dto.Expires != 0 {
			c.Expires = time.Unix(dto.Expires, 0)
		}
		jar.SetCookies(&url.URL{Scheme: "https", Host: "www.imsnsit.org", Path: dto.Path}, []*http.Cookie{c})
	}
	return nil
}

func clearCookies() {
	os.Remove(cookieFile)
}

func solveCaptcha(captchaBytes []byte) string {
	result := solveCaptchaGo(captchaBytes)
	log.Printf("CAPTCHA solved (Python ddddocr via subprocess): %q (len=%d)", result, len(result))
	return result
}

func extractSelectValue(html, name string) string {
	re := regexp.MustCompile(`<select[^>]+(?:name|id)=['"]` + regexp.QuoteMeta(name) + `['"][^>]*>(.*?)</select>`)
	match := re.FindStringSubmatch(html)
	if len(match) < 2 {
		return ""
	}
	selectHTML := match[1]

	selectedRe := regexp.MustCompile(`<option[^>]*value=['"]([^'"]+)['"][^>]*selected`)
	sm := selectedRe.FindStringSubmatch(selectHTML)
	if len(sm) >= 2 {
		val := strings.TrimSpace(sm[1])
		if val != "" {
			return val
		}
	}

	firstRe := regexp.MustCompile(`<option[^>]*value=['"]([^'"]+)['"]`)
	fm := firstRe.FindStringSubmatch(selectHTML)
	if len(fm) >= 2 {
		return strings.TrimSpace(fm[1])
	}
	return ""
}

func isSubjectCodeStrict(text string) bool {
	raw := strings.ToUpper(strings.TrimSpace(text))
	if raw == "" {
		return false
	}
	code := regexp.MustCompile(`[^A-Z0-9]`).ReplaceAllString(raw, "")
	if len(code) < 5 {
		return false
	}
	hasLetter := regexp.MustCompile(`[A-Z]`).MatchString(code)
	hasDigit := regexp.MustCompile(`\d`).MatchString(code)
	return hasLetter && hasDigit && regexp.MustCompile(`^[A-Z]{2,}[A-Z0-9]*\d{2,}$`).MatchString(code)
}

func parseInt(s string) int {
	re := regexp.MustCompile(`\d+`)
	match := re.FindString(s)
	if match == "" {
		return 0
	}
	val := 0
	fmt.Sscanf(match, "%d", &val)
	return val
}

func countBinaryMarks(s string) (int, int, int) {
	present := 0
	absent := 0
	total := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '1' {
			if i == 0 || s[i-1] < '0' || s[i-1] > '9' {
				present++
				total++
			}
		} else if s[i] == '0' {
			if i == 0 || s[i-1] < '0' || s[i-1] > '9' {
				absent++
				total++
			}
		}
	}
	return total, present, absent
}

func extractTimeline(html string) map[string][]map[string]string {
	doc, err := goquery.NewDocumentFromReader(strings.NewReader(html))
	if err != nil {
		return nil
	}

	timeline := make(map[string][]map[string]string)

	doc.Find("table").Each(func(i int, table *goquery.Selection) {
		rows := table.Find("tr")
		if rows.Length() < 2 {
			return
		}

		var headerTexts []string
		rows.Eq(0).Find("th, td").Each(func(j int, cell *goquery.Selection) {
			headerTexts = append(headerTexts, strings.ToLower(strings.TrimSpace(cell.Text())))
		})

		firstHeader := ""
		if len(headerTexts) > 0 {
			firstHeader = headerTexts[0]
		}
		if firstHeader != "days" && firstHeader != "day" && firstHeader != "date" {
			return
		}

		var subjectCodes []string
		var codeColIndices []int
		for j, h := range headerTexts[1:] {
			if isSubjectCodeStrict(h) {
				subjectCodes = append(subjectCodes, strings.TrimSpace(h))
				codeColIndices = append(codeColIndices, j+1)
			}
		}

		if len(subjectCodes) < 2 {
			return
		}

		for _, c := range subjectCodes {
			timeline[c] = []map[string]string{}
		}

		for dataRow := 1; dataRow < rows.Length(); dataRow++ {
			dcells := rows.Eq(dataRow).Find("th, td")
			dtexts := []string{}
			dcells.Each(func(k int, cell *goquery.Selection) {
				dtexts = append(dtexts, strings.TrimSpace(cell.Text()))
			})

			if len(dtexts) == 0 {
				continue
			}

			firstText := strings.TrimSpace(dtexts[0])
			firstLower := strings.ToLower(firstText)
			if firstLower == "overall" || firstLower == "total" || firstLower == "legend" || firstLower == "note" || strings.Contains(firstText, "->") {
				continue
			}

			dateLabel := firstText
			if dateLabel == "" {
				continue
			}

			for idx, colIdx := range codeColIndices {
				if colIdx >= len(dtexts) {
					continue
				}
				raw := strings.TrimSpace(dtexts[colIdx])
				status := classifyTimelineStatus(raw)
				if status == "" {
					continue
				}
				timeline[subjectCodes[idx]] = append(timeline[subjectCodes[idx]], map[string]string{
					"date":   dateLabel,
					"status": status,
					"raw":    raw,
				})
			}
		}

		for k, v := range timeline {
			if len(v) == 0 {
				delete(timeline, k)
			}
		}
	})

	if len(timeline) == 0 {
		return nil
	}
	return timeline
}

func classifyTimelineStatus(raw string) string {
	lower := strings.ToLower(strings.TrimSpace(raw))
	if lower == "p" || lower == "present" || lower == "1" {
		return "present"
	}
	if lower == "a" || lower == "absent" || lower == "0" {
		return "absent"
	}
	if lower == "h" || lower == "half" || lower == "0.5" {
		return "half"
	}
	return ""
}

func extractSubjectNames(html string) map[string]string {
	doc, err := goquery.NewDocumentFromReader(strings.NewReader(html))
	if err != nil {
		return nil
	}

	names := make(map[string]string)

	doc.Find("table").Each(func(i int, table *goquery.Selection) {
		rows := table.Find("tr")
		if rows.Length() < 2 {
			return
		}

		var headerTexts []string
		rows.Eq(0).Find("th, td").Each(func(j int, cell *goquery.Selection) {
			headerTexts = append(headerTexts, strings.TrimSpace(cell.Text()))
		})

		var subjectCodes []string
		var codeColIndices []int
		for j, h := range headerTexts[1:] {
			if isSubjectCodeStrict(h) {
				subjectCodes = append(subjectCodes, strings.TrimSpace(h))
				codeColIndices = append(codeColIndices, j+1)
			}
		}

		if len(subjectCodes) < 2 {
			return
		}

		for dataRow := 1; dataRow < rows.Length(); dataRow++ {
			dcells := rows.Eq(dataRow).Find("th, td")
			dtexts := []string{}
			dcells.Each(func(k int, cell *goquery.Selection) {
				dtexts = append(dtexts, strings.TrimSpace(cell.Text()))
			})

			if len(dtexts) == 0 {
				continue
			}

			firstLower := strings.ToLower(strings.TrimSpace(dtexts[0]))
			if firstLower == "days" || firstLower == "day" || firstLower == "date" || firstLower == "overall" || firstLower == "total" || firstLower == "legend" || firstLower == "note" || strings.Contains(dtexts[0], "->") {
				continue
			}

			for _, colIdx := range codeColIndices {
				if colIdx >= len(dtexts) {
					continue
				}
				cellText := dtexts[colIdx]
				codeRe := regexp.MustCompile(`\b([A-Z]{2,}[A-Z0-9]*\d{2,})\b`)
				codeMatch := codeRe.FindString(cellText)
				if codeMatch == "" {
					continue
				}
				code := strings.ToUpper(codeMatch)

				nameRe := regexp.MustCompile(`\b([A-Z]{2,}[A-Z0-9]*\d{2,})\s*-\s*(.+?)(?=\s+[A-Z]{2,}[A-Z0-9]*\d{2,}\s*-|$)`)
				nameMatch := nameRe.FindStringSubmatch(cellText)
				if len(nameMatch) >= 3 {
					name := strings.TrimSpace(nameMatch[2])
					name = strings.Trim(name, " -:;,")
					if name != "" && !strings.Contains(name, "--->") && !strings.HasPrefix(name, ">") {
						names[code] = name
					}
				}
			}
		}
	})

	return names
}

func newRequest(method, url string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequest(method, url, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("sec-ch-ua", `"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"`)
	req.Header.Set("sec-ch-ua-mobile", "?0")
	req.Header.Set("sec-ch-ua-platform", `"Windows"`)
	return req, nil
}

func parseAttendanceTable(html string) map[string]map[string]int {
	doc, err := goquery.NewDocumentFromReader(strings.NewReader(html))
	if err != nil {
		return nil
	}

	data := make(map[string]map[string]int)

	doc.Find("table").Each(func(i int, table *goquery.Selection) {
		rows := table.Find("tr")
		if rows.Length() < 2 {
			return
		}

		for rowIdx := 0; rowIdx < rows.Length() && rowIdx < 8; rowIdx++ {
			row := rows.Eq(rowIdx)
			cells := row.Find("th, td")
			var texts []string
			cells.Each(func(j int, cell *goquery.Selection) {
				texts = append(texts, strings.ToLower(strings.TrimSpace(cell.Text())))
			})
			if len(texts) < 3 {
				continue
			}

			first := texts[0]
			if first != "days" && first != "day" && first != "date" {
				continue
			}

			var subjectCodes []string
			var codeColIndices []int
			for j := 1; j < len(texts); j++ {
				if isSubjectCodeStrict(texts[j]) {
					subjectCodes = append(subjectCodes, strings.ToUpper(strings.TrimSpace(texts[j])))
					codeColIndices = append(codeColIndices, j)
				}
			}

			if len(subjectCodes) < 2 {
				continue
			}

			agg := make(map[string]map[string]int)
			for _, code := range subjectCodes {
				agg[code] = map[string]int{"total": 0, "present": 0, "absent": 0}
			}

			overallClasses := make(map[string]int)
			overallPresent := make(map[string]int)
			overallAbsent := make(map[string]int)
			totalClasses := make(map[string]int)
			totalPresent := make(map[string]int)
			totalAbsent := make(map[string]int)

			for dataRowIdx := rowIdx + 1; dataRowIdx < rows.Length(); dataRowIdx++ {
				drow := rows.Eq(dataRowIdx)
				dcells := drow.Find("th, td")
				var dtexts []string
				dcells.Each(func(k int, cell *goquery.Selection) {
					dtexts = append(dtexts, strings.TrimSpace(cell.Text()))
				})
				if len(dtexts) == 0 {
					continue
				}

				firstLower := strings.ToLower(dtexts[0])

				if strings.Contains(firstLower, "overall") && strings.Contains(firstLower, "class") {
					for k, colIdx := range codeColIndices {
						if colIdx < len(dtexts) {
							overallClasses[subjectCodes[k]] = parseInt(dtexts[colIdx])
						}
					}
					continue
				}
				if strings.Contains(firstLower, "overall") && strings.Contains(firstLower, "present") {
					for k, colIdx := range codeColIndices {
						if colIdx < len(dtexts) {
							overallPresent[subjectCodes[k]] = parseInt(dtexts[colIdx])
						}
					}
					continue
				}
				if strings.Contains(firstLower, "overall") && strings.Contains(firstLower, "absent") {
					for k, colIdx := range codeColIndices {
						if colIdx < len(dtexts) {
							overallAbsent[subjectCodes[k]] = parseInt(dtexts[colIdx])
						}
					}
					continue
				}
				if strings.Contains(firstLower, "total") && strings.Contains(firstLower, "class") {
					for k, colIdx := range codeColIndices {
						if colIdx < len(dtexts) {
							totalClasses[subjectCodes[k]] = parseInt(dtexts[colIdx])
						}
					}
					continue
				}
				if strings.Contains(firstLower, "total") && strings.Contains(firstLower, "present") {
					for k, colIdx := range codeColIndices {
						if colIdx < len(dtexts) {
							totalPresent[subjectCodes[k]] = parseInt(dtexts[colIdx])
						}
					}
					continue
				}
				if strings.Contains(firstLower, "total") && strings.Contains(firstLower, "absent") {
					for k, colIdx := range codeColIndices {
						if colIdx < len(dtexts) {
							totalAbsent[subjectCodes[k]] = parseInt(dtexts[colIdx])
						}
					}
					continue
				}

				if strings.Contains(dtexts[0], "->") || strings.Contains(firstLower, "legend") || strings.Contains(firstLower, "note") {
					continue
				}

				for k, colIdx := range codeColIndices {
					if colIdx >= len(dtexts) {
						continue
					}
					held, attended, absent := countBinaryMarks(dtexts[colIdx])
					code := subjectCodes[k]
					agg[code]["total"] += held
					agg[code]["present"] += attended
					agg[code]["absent"] += absent
				}
			}

			if len(overallClasses) > 0 {
				for _, code := range subjectCodes {
					total := overallClasses[code]
					if total <= 0 {
						continue
					}
					present := overallPresent[code]
					if present == 0 {
						present = agg[code]["present"]
					}
					absent := overallAbsent[code]
					if absent == 0 {
						absent = total - present
						if absent < 0 {
							absent = 0
						}
					}
					data[code] = map[string]int{"total": total, "present": present, "absent": absent}
				}
				return
			}

			if len(totalClasses) > 0 {
				for _, code := range subjectCodes {
					total := totalClasses[code]
					if total <= 0 {
						continue
					}
					present := totalPresent[code]
					if present == 0 {
						present = agg[code]["present"]
					}
					absent := totalAbsent[code]
					if absent == 0 {
						absent = total - present
						if absent < 0 {
							absent = 0
						}
					}
					data[code] = map[string]int{"total": total, "present": present, "absent": absent}
				}
				return
			}

			for _, code := range subjectCodes {
				if agg[code]["total"] > 0 {
					data[code] = agg[code]
				}
			}
			return
		}
	})

	return data
}

func fetchAttendance(client *http.Client, userID, password, year, semester string) ScrapeResult {
	start := time.Now()
	phaseStart := start

	hasCookies := false
	if client.Jar != nil {
		cookies := client.Jar.Cookies(&url.URL{Scheme: "https", Host: "www.imsnsit.org", Path: "/"})
		hasCookies = len(cookies) > 0
	}

	if !hasCookies {
		if loadErr := loadCookies(client); loadErr == nil {
			result := tryFetchWithCachedSession(client, userID, year, semester, start)
			if result.Status == "success" {
				return result
			}
			clearCookies()
		}
	} else {
		result := tryFetchWithCachedSession(client, userID, year, semester, start)
		if result.Status == "success" {
			return result
		}
		clearCookies()
	}

	for attempt := 1; attempt <= maxRetries; attempt++ {
		phaseStart = time.Now()
		// Step 1: Initialize session
		req, err := newRequest("GET", imsBase, nil)
		if err != nil {
			continue
		}
		resp, err := client.Do(req)
		if err != nil {
			continue
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()

		// Step 2: Plum5 login page
		req, err = newRequest("GET", imsBase+"plum5_fw_login.php?t=sw&w=1", nil)
		if err != nil {
			continue
		}
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()

		// Step 3: Student page
		req, err = newRequest("GET", imsBase+"student.htm", nil)
		if err != nil {
			continue
		}
		req.Header.Set("Referer", imsBase)
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()

		// Step 4: Student login 110
		req, err = newRequest("GET", imsBase+"student_login110.php", nil)
		if err != nil {
			continue
		}
		req.Header.Set("Referer", imsBase+"student.htm")
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()

		// Step 5: Get login page
		req, err = newRequest("GET", imsBase+"student_login.php", nil)
		if err != nil {
			continue
		}
		req.Header.Set("Referer", imsBase+"student.htm")
		req.Header.Set("Upgrade-Insecure-Requests", "1")
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		loginHTML, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}

		loginStr := string(loginHTML)
		fmt.Fprintf(os.Stderr, "[PHASE] Session init (5 HTTP requests): %dms\n", time.Since(phaseStart).Milliseconds())
		phaseStart = time.Now()

		// Extract form fields
		fyRe := regexp.MustCompile(`name='fy' id='fy' value='([^']+)'`)
		compRe := regexp.MustCompile(`name='comp' id='comp' type='hidden' readonly value='([^']+)'`)
		hrandRe := regexp.MustCompile(`name='HRAND_NUM' id='HRAND_NUM' value='([^']+)'`)
		capsrcRe := regexp.MustCompile(`<img src='([^']+captcha[^']+)' id='captchaimg'`)

		fyMatch := fyRe.FindStringSubmatch(loginStr)
		compMatch := compRe.FindStringSubmatch(loginStr)
		hrandMatch := hrandRe.FindStringSubmatch(loginStr)
		capsrcMatch := capsrcRe.FindStringSubmatch(loginStr)


		if len(fyMatch) < 2 || len(compMatch) < 2 || len(hrandMatch) < 2 || len(capsrcMatch) < 2 {
			continue
		}

		fy := fyMatch[1]
		comp := compMatch[1]
		hrand := hrandMatch[1]
		capsrc := capsrcMatch[1]

		// Step 6: Get CAPTCHA
		captchaURL := imsBase + capsrc
		req, err = newRequest("GET", captchaURL, nil)
		if err != nil {
			continue
		}
		req.Header.Set("Referer", imsBase+"student_login.php")
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		captchaBytes, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}

		captchaText := solveCaptcha(captchaBytes)
		fmt.Fprintf(os.Stderr, "[PHASE] CAPTCHA download+solve: %dms\n", time.Since(phaseStart).Milliseconds())
		if len(captchaText) < 4 {
			continue
		}
		phaseStart = time.Now()

		// Step 7: Login
		loginData := url.Values{}
		loginData.Set("f", "")
		loginData.Set("uid", userID)
		loginData.Set("pwd", password)
		loginData.Set("HRAND_NUM", hrand)
		loginData.Set("fy", fy)
		loginData.Set("comp", comp)
		loginData.Set("cap", captchaText)
		loginData.Set("logintype", "student")

		req, err = newRequest("POST", imsBase+"student_login.php", strings.NewReader(loginData.Encode()))
		if err != nil {
			continue
		}
		req.Header.Set("Referer", imsBase+"student_login.php")
		req.Header.Set("Origin", "https://www.imsnsit.org")
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		req.Header.Set("Upgrade-Insecure-Requests", "1")
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		bannerHTML, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}

		bannerStr := string(bannerHTML)
		bannerLower := strings.ToLower(bannerStr)
		if len(bannerStr) > 1000 {
		} else {
		}

		if strings.Contains(bannerLower, "invalid security") || strings.Contains(bannerLower, "please login") {
			continue
		}
		if !strings.Contains(bannerLower, "logout") || !strings.Contains(bannerLower, "my activities") {
			continue
		}

		// Save session cookies after successful login
		saveCookies(client)
		fmt.Fprintf(os.Stderr, "[PHASE] Login form submit: %dms\n", time.Since(phaseStart).Milliseconds())
		phaseStart = time.Now()

		// Step 8: Get My Activities
		myActivitiesRe := regexp.MustCompile(`href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Activities<`)
		myActivitiesMatch := myActivitiesRe.FindStringSubmatch(string(bannerHTML))
		if len(myActivitiesMatch) < 2 {
			// Print more of the banner for debugging
			if len(bannerStr) > 2000 {
			} else {
			}
			return ScrapeResult{Status: "navigation_failed", ElapsedMs: time.Since(start).Milliseconds()}
		}

		myActivitiesURL := myActivitiesMatch[1]
		req, err = newRequest("GET", myActivitiesURL, nil)
		if err != nil {
			continue
		}
		req.Header.Set("Referer", imsBase+"student_login.php")
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		menuHTML, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}
		fmt.Fprintf(os.Stderr, "[PHASE] My Activities navigation: %dms\n", time.Since(phaseStart).Milliseconds())
		phaseStart = time.Now()

		// Step 9: Get My Attendance
		attendanceRe := regexp.MustCompile(`href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Attendance<`)
		attendanceMatch := attendanceRe.FindStringSubmatch(string(menuHTML))
		if len(attendanceMatch) < 2 {
			return ScrapeResult{Status: "navigation_failed", ElapsedMs: time.Since(start).Milliseconds()}
		}

		attendanceURL := attendanceMatch[1]
		req, err = newRequest("GET", attendanceURL, nil)
		if err != nil {
			continue
		}
		req.Header.Set("Referer", myActivitiesURL)
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		attendanceHTML, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}

		attendanceStr := string(attendanceHTML)
		fmt.Fprintf(os.Stderr, "[PHASE] My Attendance navigation: %dms\n", time.Since(phaseStart).Milliseconds())
		phaseStart = time.Now()

		initialYear := extractSelectValue(attendanceStr, "year")
		initialSem := extractSelectValue(attendanceStr, "sem")
		resolvedYear := year
		if resolvedYear == "" {
			resolvedYear = initialYear
		}
		resolvedSemester := semester
		if resolvedSemester == "" {
			resolvedSemester = initialSem
		}

		encYearRe := regexp.MustCompile(`name='enc_year' id='enc_year' value='([^']+)'`)
		encSemRe := regexp.MustCompile(`name='enc_sem' id='enc_sem' value='([^']+)'`)
		recentityRe := regexp.MustCompile(`name=recentitycode value='([^']+)'`)
		deptRe := regexp.MustCompile(`name=dept value='([^']+)'`)
		degreeRe := regexp.MustCompile(`name=degree value='([^']+)'`)

		encYear := encYearRe.FindStringSubmatch(attendanceStr)
		encSem := encSemRe.FindStringSubmatch(attendanceStr)
		recentity := recentityRe.FindStringSubmatch(attendanceStr)
		dept := deptRe.FindStringSubmatch(attendanceStr)
		degree := degreeRe.FindStringSubmatch(attendanceStr)


		if len(encYear) < 2 || len(encSem) < 2 || len(recentity) < 2 || len(dept) < 2 || len(degree) < 2 {
			return ScrapeResult{Status: "navigation_failed", ElapsedMs: time.Since(start).Milliseconds()}
		}

		// Step 10: Submit year/semester
		attendanceData := url.Values{}
		attendanceData.Set("year", resolvedYear)
		attendanceData.Set("enc_year", encYear[1])
		attendanceData.Set("sem", resolvedSemester)
		attendanceData.Set("enc_sem", encSem[1])
		attendanceData.Set("submit", "Submit")
		attendanceData.Set("recentitycode", recentity[1])
		attendanceData.Set("dept", dept[1])
		attendanceData.Set("degree", degree[1])
		attendanceData.Set("ename", "")
		attendanceData.Set("ecode", "")

		req, err = newRequest("POST", attendanceURL, strings.NewReader(attendanceData.Encode()))
		if err != nil {
			continue
		}
		req.Header.Set("Referer", attendanceURL)
		req.Header.Set("Origin", "https://www.imsnsit.org")
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		req.Header.Set("Upgrade-Insecure-Requests", "1")
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		resultHTML, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}

		resultStr := string(resultHTML)
		fmt.Fprintf(os.Stderr, "[PHASE] Year/Sem submit: %dms\n", time.Since(phaseStart).Milliseconds())
		phaseStart = time.Now()
		if len(resultStr) > 5000 {
		} else {
		}
		if resultStr == "" {
			return ScrapeResult{Status: "navigation_failed", ElapsedMs: time.Since(start).Milliseconds()}
		}

		// Parse attendance table
		doc, err := goquery.NewDocumentFromReader(strings.NewReader(resultStr))
		if err != nil {
			continue
		}

		fmt.Fprintf(os.Stderr, "[PHASE] Attendance page HTML fetch: %dms\n", time.Since(phaseStart).Milliseconds())
		phaseStart = time.Now()

		data := make(map[string]map[string]int)

		doc.Find("table").Each(func(i int, table *goquery.Selection) {
			rows := table.Find("tr")
			if rows.Length() < 2 {
				return
			}

			// Strategy: Look for "Days" header row with subject codes as columns
			for rowIdx := 0; rowIdx < rows.Length() && rowIdx < 8; rowIdx++ {
				row := rows.Eq(rowIdx)
				cells := row.Find("th, td")
				var texts []string
				cells.Each(func(j int, cell *goquery.Selection) {
					texts = append(texts, strings.ToLower(strings.TrimSpace(cell.Text())))
				})
				if len(texts) < 3 {
					continue
				}

				first := texts[0]
				if first != "days" && first != "day" && first != "date" {
					continue
				}

				// Found "Days" header row - extract subject codes from columns
				var subjectCodes []string
				var codeColIndices []int
				for j := 1; j < len(texts); j++ {
					if isSubjectCodeStrict(texts[j]) {
						subjectCodes = append(subjectCodes, strings.ToUpper(strings.TrimSpace(texts[j])))
						codeColIndices = append(codeColIndices, j)
					}
				}

				if len(subjectCodes) < 2 {
					continue
				}


				// Initialize aggregates
				agg := make(map[string]map[string]int)
				for _, code := range subjectCodes {
					agg[code] = map[string]int{"total": 0, "present": 0, "absent": 0}
				}

				overallClasses := make(map[string]int)
				overallPresent := make(map[string]int)
				overallAbsent := make(map[string]int)
				totalClasses := make(map[string]int)
				totalPresent := make(map[string]int)
				totalAbsent := make(map[string]int)

				// Process data rows
				for dataRowIdx := rowIdx + 1; dataRowIdx < rows.Length(); dataRowIdx++ {
					drow := rows.Eq(dataRowIdx)
					dcells := drow.Find("th, td")
					var dtexts []string
					dcells.Each(func(k int, cell *goquery.Selection) {
						dtexts = append(dtexts, strings.TrimSpace(cell.Text()))
					})
					if len(dtexts) == 0 {
						continue
					}

					firstLower := strings.ToLower(dtexts[0])

					// Summary rows
					if strings.Contains(firstLower, "overall") && strings.Contains(firstLower, "class") {
						for k, colIdx := range codeColIndices {
							if colIdx < len(dtexts) {
								overallClasses[subjectCodes[k]] = parseInt(dtexts[colIdx])
							}
						}
						continue
					}
					if strings.Contains(firstLower, "overall") && strings.Contains(firstLower, "present") {
						for k, colIdx := range codeColIndices {
							if colIdx < len(dtexts) {
								overallPresent[subjectCodes[k]] = parseInt(dtexts[colIdx])
							}
						}
						continue
					}
					if strings.Contains(firstLower, "overall") && strings.Contains(firstLower, "absent") {
						for k, colIdx := range codeColIndices {
							if colIdx < len(dtexts) {
								overallAbsent[subjectCodes[k]] = parseInt(dtexts[colIdx])
							}
						}
						continue
					}
					if strings.Contains(firstLower, "total") && strings.Contains(firstLower, "class") {
						for k, colIdx := range codeColIndices {
							if colIdx < len(dtexts) {
								totalClasses[subjectCodes[k]] = parseInt(dtexts[colIdx])
							}
						}
						continue
					}
					if strings.Contains(firstLower, "total") && strings.Contains(firstLower, "present") {
						for k, colIdx := range codeColIndices {
							if colIdx < len(dtexts) {
								totalPresent[subjectCodes[k]] = parseInt(dtexts[colIdx])
							}
						}
						continue
					}
					if strings.Contains(firstLower, "total") && strings.Contains(firstLower, "absent") {
						for k, colIdx := range codeColIndices {
							if colIdx < len(dtexts) {
								totalAbsent[subjectCodes[k]] = parseInt(dtexts[colIdx])
							}
						}
						continue
					}

					// Skip notes/legend
					if strings.Contains(dtexts[0], "->") || strings.Contains(firstLower, "legend") || strings.Contains(firstLower, "note") {
						continue
					}

					// Daily rows: count 1/0 marks per subject
					for k, colIdx := range codeColIndices {
						if colIdx >= len(dtexts) {
							continue
						}
						held, attended, absent := countBinaryMarks(dtexts[colIdx])
						code := subjectCodes[k]
						agg[code]["total"] += held
						agg[code]["present"] += attended
						agg[code]["absent"] += absent
					}
				}

				// Prefer explicit OVERALL totals
				if len(overallClasses) > 0 {
					for _, code := range subjectCodes {
						total := overallClasses[code]
						if total <= 0 {
							continue
						}
						present := overallPresent[code]
						if present == 0 {
							present = agg[code]["present"]
						}
						absent := overallAbsent[code]
						if absent == 0 {
							absent = total - present
							if absent < 0 {
								absent = 0
							}
						}
						data[code] = map[string]int{"total": total, "present": present, "absent": absent}
					}
					return // Found valid table
				}

				// Otherwise use monthly TOTAL rows
				if len(totalClasses) > 0 {
					for _, code := range subjectCodes {
						total := totalClasses[code]
						if total <= 0 {
							continue
						}
						present := totalPresent[code]
						if present == 0 {
							present = agg[code]["present"]
						}
						absent := totalAbsent[code]
						if absent == 0 {
							absent = total - present
							if absent < 0 {
								absent = 0
							}
						}
						data[code] = map[string]int{"total": total, "present": present, "absent": absent}
					}
					return // Found valid table
				}

				// Fallback to aggregated daily counts
				for _, code := range subjectCodes {
					if agg[code]["total"] > 0 {
						data[code] = agg[code]
					}
				}
				return // Found valid table
			}
		})

		// Extract timeline and subject names
		timeline := extractTimeline(resultStr)
		subjectNames := extractSubjectNames(resultStr)

		fmt.Fprintf(os.Stderr, "[PHASE] HTML parse+timeline+names: %dms\n", time.Since(phaseStart).Milliseconds())
		fmt.Fprintf(os.Stderr, "[PHASE] TOTAL: %dms\n", time.Since(start).Milliseconds())

		elapsed := time.Since(start).Milliseconds()
		status := "success"
		if len(data) == 0 {
			status = "navigation_failed"
		}

		return ScrapeResult{
			Data:         data,
			Timeline:     timeline,
			SubjectNames: subjectNames,
			Status:       status,
			ElapsedMs:    elapsed,
		}
	}

	return ScrapeResult{
		Data:         make(map[string]map[string]int),
		Timeline:     make(map[string][]map[string]string),
		SubjectNames: make(map[string]string),
		Status:       "unknown_error",
		ElapsedMs:    time.Since(start).Milliseconds(),
	}
}

func tryFetchWithCachedSession(client *http.Client, userID, year, semester string, start time.Time) ScrapeResult {
	req, err := newRequest("GET", imsBase, nil)
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}
	resp, err := client.Do(req)
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()

	req, err = newRequest("GET", imsBase+"student.htm", nil)
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}
	resp, err = client.Do(req)
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}
	html, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}

	htmlStr := string(html)
	if strings.Contains(strings.ToLower(htmlStr), "login") || strings.Contains(strings.ToLower(htmlStr), "student_login") {
		return ScrapeResult{Status: "navigation_failed"}
	}

	// Try to navigate to attendance directly
	attendanceRe := regexp.MustCompile(`href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Attendance<`)
	attendanceMatch := attendanceRe.FindStringSubmatch(htmlStr)
	if len(attendanceMatch) < 2 {
		return ScrapeResult{Status: "navigation_failed"}
	}

	attendanceURL := attendanceMatch[1]
	req, err = newRequest("GET", attendanceURL, nil)
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}
	req.Header.Set("Referer", imsBase+"student.htm")
	resp, err = client.Do(req)
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}
	attendanceHTML, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}

	attendanceStr := string(attendanceHTML)

	initialYear := extractSelectValue(attendanceStr, "year")
	initialSem := extractSelectValue(attendanceStr, "sem")
	resolvedYear := year
	if resolvedYear == "" {
		resolvedYear = initialYear
	}
	resolvedSemester := semester
	if resolvedSemester == "" {
		resolvedSemester = initialSem
	}

	encYearRe := regexp.MustCompile(`name='enc_year' id='enc_year' value='([^']+)'`)
	encSemRe := regexp.MustCompile(`name='enc_sem' id='enc_sem' value='([^']+)'`)
	recentityRe := regexp.MustCompile(`name=recentitycode value='([^']+)'`)
	deptRe := regexp.MustCompile(`name=dept value='([^']+)'`)
	degreeRe := regexp.MustCompile(`name=degree value='([^']+)'`)

	encYear := encYearRe.FindStringSubmatch(attendanceStr)
	encSem := encSemRe.FindStringSubmatch(attendanceStr)
	recentity := recentityRe.FindStringSubmatch(attendanceStr)
	dept := deptRe.FindStringSubmatch(attendanceStr)
	degree := degreeRe.FindStringSubmatch(attendanceStr)

	if len(encYear) < 2 || len(encSem) < 2 || len(recentity) < 2 || len(dept) < 2 || len(degree) < 2 {
		return ScrapeResult{Status: "navigation_failed"}
	}

	attendanceData := url.Values{}
	attendanceData.Set("year", resolvedYear)
	attendanceData.Set("enc_year", encYear[1])
	attendanceData.Set("sem", resolvedSemester)
	attendanceData.Set("enc_sem", encSem[1])
	attendanceData.Set("submit", "Submit")
	attendanceData.Set("recentitycode", recentity[1])
	attendanceData.Set("dept", dept[1])
	attendanceData.Set("degree", degree[1])
	attendanceData.Set("ename", "")
	attendanceData.Set("ecode", "")

	req, err = newRequest("POST", attendanceURL, strings.NewReader(attendanceData.Encode()))
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}
	req.Header.Set("Referer", attendanceURL)
	req.Header.Set("Origin", "https://www.imsnsit.org")
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Upgrade-Insecure-Requests", "1")
	resp, err = client.Do(req)
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}
	resultHTML, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		return ScrapeResult{Status: "unknown_error"}
	}

	resultStr := string(resultHTML)
	if resultStr == "" {
		return ScrapeResult{Status: "navigation_failed"}
	}

	doc, err := goquery.NewDocumentFromReader(strings.NewReader(resultStr))
	if err != nil {
		return ScrapeResult{Status: "navigation_failed"}
	}

	data := make(map[string]map[string]int)

	doc.Find("table").Each(func(i int, table *goquery.Selection) {
		rows := table.Find("tr")
		if rows.Length() < 2 {
			return
		}

		var headers []string
		rows.Eq(0).Find("th, td").Each(func(j int, cell *goquery.Selection) {
			headers = append(headers, strings.ToLower(strings.TrimSpace(cell.Text())))
		})

		subjectIdx := -1
		presentIdx := -1
		absentIdx := -1
		totalIdx := -1

		for idx, h := range headers {
			if strings.Contains(h, "subject") && strings.Contains(h, "code") {
				subjectIdx = idx
			}
			if strings.Contains(h, "present") {
				presentIdx = idx
			}
			if strings.Contains(h, "absent") {
				absentIdx = idx
			}
			if strings.Contains(h, "total") {
				totalIdx = idx
			}
		}

		if subjectIdx < 0 {
			return
		}

		rows.Each(func(rowIdx int, row *goquery.Selection) {
			if rowIdx == 0 {
				return
			}

			var cells []string
			row.Find("td").Each(func(j int, cell *goquery.Selection) {
				cells = append(cells, strings.TrimSpace(cell.Text()))
			})

			if len(cells) <= subjectIdx {
				return
			}

			cellText := cells[subjectIdx]
			codeRe := regexp.MustCompile(`\b([A-Z]{2,}[A-Z0-9]*\d{2,})\b`)
			codeMatch := codeRe.FindString(cellText)
			if codeMatch == "" {
				return
			}

			code := strings.ToUpper(codeMatch)
			present := 0
			absent := 0
			total := 0

			if presentIdx >= 0 && presentIdx < len(cells) {
				fmt.Sscanf(cells[presentIdx], "%d", &present)
			}
			if absentIdx >= 0 && absentIdx < len(cells) {
				fmt.Sscanf(cells[absentIdx], "%d", &absent)
			}
			if totalIdx >= 0 && totalIdx < len(cells) {
				fmt.Sscanf(cells[totalIdx], "%d", &total)
			}

			entry := map[string]int{
				"present": present,
				"absent":  absent,
				"total":   total,
			}
			data[code] = entry
		})
	})

	timeline := extractTimeline(resultStr)
	subjectNames := extractSubjectNames(resultStr)

	elapsed := time.Since(start).Milliseconds()
	status := "success"
	if len(data) == 0 {
		status = "navigation_failed"
	}

	return ScrapeResult{
		Data:         data,
		Timeline:     timeline,
		SubjectNames: subjectNames,
		Status:       status,
		ElapsedMs:    elapsed,
	}
}

type ConcurrentResult struct {
	UserID string
	Result ScrapeResult
	Error  error
}

func fetchAttendanceConcurrent(userIDs []string, password, year, semester string, concurrency int) []ConcurrentResult {
	results := make([]ConcurrentResult, len(userIDs))
	var wg sync.WaitGroup
	sem := make(chan struct{}, concurrency)

	for i, uid := range userIDs {
		wg.Add(1)
		go func(idx int, userID string) {
			sem <- struct{}{}
			defer wg.Done()
			defer func() { <-sem }()

			jar, _ := cookiejar.New(nil)
			transport := &http.Transport{
				MaxIdleConns:        10,
				MaxIdleConnsPerHost: 5,
				IdleConnTimeout:     90 * time.Second,
				TLSHandshakeTimeout: 10 * time.Second,
			}
			client := &http.Client{
				Jar:       jar,
				Timeout:   httpTimeout,
				Transport: transport,
			}

			result := fetchAttendance(client, userID, password, year, semester)
			results[idx] = ConcurrentResult{
				UserID: userID,
				Result: result,
				Error:  nil,
			}
			log.Printf("User %s: status=%s, elapsed=%dms, subjects=%d", userID, result.Status, result.ElapsedMs, len(result.Data))
		}(i, uid)
	}

	wg.Wait()
	return results
}

func loginToIMS(client *http.Client) (string, error) {
	imsSteps := []string{
		imsBase,
		imsBase + "plum5_fw_login.php?t=sw&w=1",
		imsBase + "student.htm",
		imsBase + "student_login110.php",
	}
	for _, stepURL := range imsSteps {
		req, err := newRequest("GET", stepURL, nil)
		if err != nil {
			return "", err
		}
		resp, err := client.Do(req)
		if err != nil {
			return "", err
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}

	req, err := newRequest("GET", imsBase+"student_login.php", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Referer", imsBase+"student.htm")
	req.Header.Set("Upgrade-Insecure-Requests", "1")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	loginHTML, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		return "", err
	}
	loginStr := string(loginHTML)

	fyRe := regexp.MustCompile(`name='fy' id='fy' value='([^']+)'`)
	compRe := regexp.MustCompile(`name='comp' id='comp' type='hidden' readonly value='([^']+)'`)
	hrandRe := regexp.MustCompile(`name='HRAND_NUM' id='HRAND_NUM' value='([^']+)'`)
	capsrcRe := regexp.MustCompile(`<img src='([^']+captcha[^']+)' id='captchaimg'`)

	fyMatch := fyRe.FindStringSubmatch(loginStr)
	compMatch := compRe.FindStringSubmatch(loginStr)
	hrandMatch := hrandRe.FindStringSubmatch(loginStr)
	capsrcMatch := capsrcRe.FindStringSubmatch(loginStr)
	if len(fyMatch) < 2 || len(compMatch) < 2 || len(hrandMatch) < 2 || len(capsrcMatch) < 2 {
		return "", fmt.Errorf("form fields not found")
	}

	captchaURL := imsBase + capsrcMatch[1]
	req, err = newRequest("GET", captchaURL, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Referer", imsBase+"student_login.php")
	resp, err = client.Do(req)
	if err != nil {
		return "", err
	}
	captchaBytes, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		return "", err
	}

	captchaText := solveCaptcha(captchaBytes)
	if len(captchaText) < 4 {
		return "", fmt.Errorf("CAPTCHA solve failed")
	}

	loginData := url.Values{}
	loginData.Set("f", "")
	loginData.Set("uid", "")
	loginData.Set("pwd", "")
	loginData.Set("HRAND_NUM", hrandMatch[1])
	loginData.Set("fy", fyMatch[1])
	loginData.Set("comp", compMatch[1])
	loginData.Set("cap", captchaText)
	loginData.Set("logintype", "student")

	req, err = newRequest("POST", imsBase+"student_login.php", strings.NewReader(loginData.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Referer", imsBase+"student_login.php")
	req.Header.Set("Origin", "https://www.imsnsit.org")
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Upgrade-Insecure-Requests", "1")
	resp, err = client.Do(req)
	if err != nil {
		return "", err
	}
	bannerHTML, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		return "", err
	}

	bannerStr := string(bannerHTML)
	bannerLower := strings.ToLower(bannerStr)
	if strings.Contains(bannerLower, "invalid security") || strings.Contains(bannerLower, "please login") {
		return "", fmt.Errorf("login failed")
	}
	if !strings.Contains(bannerLower, "logout") || !strings.Contains(bannerLower, "my activities") {
		return "", fmt.Errorf("login failed - no logout/my activities")
	}

	saveCookies(client)

	myActivitiesRe := regexp.MustCompile(`href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Activities<`)
	myActivitiesMatch := myActivitiesRe.FindStringSubmatch(bannerStr)
	if len(myActivitiesMatch) < 2 {
		return "", fmt.Errorf("My Activities link not found")
	}

	req, err = newRequest("GET", myActivitiesMatch[1], nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Referer", imsBase+"student_login.php")
	resp, err = client.Do(req)
	if err != nil {
		return "", err
	}
	menuHTML, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		return "", err
	}

	return string(menuHTML), nil
}

func loginWithCredentials(client *http.Client, userID, password string) (string, error) {
	for attempt := 1; attempt <= maxRetries; attempt++ {
		req, err := newRequest("GET", imsBase, nil)
		if err != nil {
			continue
		}
		resp, err := client.Do(req)
		if err != nil {
			continue
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()

		for _, stepURL := range []string{
			imsBase + "plum5_fw_login.php?t=sw&w=1",
			imsBase + "student.htm",
			imsBase + "student_login110.php",
		} {
			req, err = newRequest("GET", stepURL, nil)
			if err != nil {
				continue
			}
			resp, err = client.Do(req)
			if err != nil {
				continue
			}
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}

		req, err = newRequest("GET", imsBase+"student_login.php", nil)
		if err != nil {
			continue
		}
		req.Header.Set("Referer", imsBase+"student.htm")
		req.Header.Set("Upgrade-Insecure-Requests", "1")
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		loginHTML, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}
		loginStr := string(loginHTML)

		fyRe := regexp.MustCompile(`name='fy' id='fy' value='([^']+)'`)
		compRe := regexp.MustCompile(`name='comp' id='comp' type='hidden' readonly value='([^']+)'`)
		hrandRe := regexp.MustCompile(`name='HRAND_NUM' id='HRAND_NUM' value='([^']+)'`)
		capsrcRe := regexp.MustCompile(`<img src='([^']+captcha[^']+)' id='captchaimg'`)

		fyMatch := fyRe.FindStringSubmatch(loginStr)
		compMatch := compRe.FindStringSubmatch(loginStr)
		hrandMatch := hrandRe.FindStringSubmatch(loginStr)
		capsrcMatch := capsrcRe.FindStringSubmatch(loginStr)
		if len(fyMatch) < 2 || len(compMatch) < 2 || len(hrandMatch) < 2 || len(capsrcMatch) < 2 {
			continue
		}

		captchaURL := imsBase + capsrcMatch[1]
		req, err = newRequest("GET", captchaURL, nil)
		if err != nil {
			continue
		}
		req.Header.Set("Referer", imsBase+"student_login.php")
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		captchaBytes, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}

		captchaText := solveCaptcha(captchaBytes)
		if len(captchaText) < 4 {
			continue
		}

		loginData := url.Values{}
		loginData.Set("f", "")
		loginData.Set("uid", userID)
		loginData.Set("pwd", password)
		loginData.Set("HRAND_NUM", hrandMatch[1])
		loginData.Set("fy", fyMatch[1])
		loginData.Set("comp", compMatch[1])
		loginData.Set("cap", captchaText)
		loginData.Set("logintype", "student")

		req, err = newRequest("POST", imsBase+"student_login.php", strings.NewReader(loginData.Encode()))
		if err != nil {
			continue
		}
		req.Header.Set("Referer", imsBase+"student_login.php")
		req.Header.Set("Origin", "https://www.imsnsit.org")
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		req.Header.Set("Upgrade-Insecure-Requests", "1")
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		bannerHTML, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}

		bannerStr := string(bannerHTML)
		bannerLower := strings.ToLower(bannerStr)
		if strings.Contains(bannerLower, "invalid security") || strings.Contains(bannerLower, "please login") {
			continue
		}
		if !strings.Contains(bannerLower, "logout") || !strings.Contains(bannerLower, "my activities") {
			continue
		}

		saveCookies(client)

		myActivitiesRe := regexp.MustCompile(`href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Activities<`)
		myActivitiesMatch := myActivitiesRe.FindStringSubmatch(bannerStr)
		if len(myActivitiesMatch) < 2 {
			return "", fmt.Errorf("My Activities link not found")
		}

		req, err = newRequest("GET", myActivitiesMatch[1], nil)
		if err != nil {
			continue
		}
		req.Header.Set("Referer", imsBase+"student_login.php")
		resp, err = client.Do(req)
		if err != nil {
			continue
		}
		menuHTML, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}
		return string(menuHTML), nil
	}
	return "", fmt.Errorf("login failed after %d attempts", maxRetries)
}

func fetchCourses(client *http.Client, userID, password string) ([]RegisteredCourse, error) {
	if loadErr := loadCookies(client); loadErr == nil {
		menuHTML, err := loginToIMS(client)
		if err == nil && menuHTML != "" {
			coursesURL := findRegisteredCoursesLink(menuHTML, imsBase)
			if coursesURL != "" {
				req, err := newRequest("GET", coursesURL, nil)
				if err == nil {
					req.Header.Set("Referer", imsBase+"student_login.php")
					resp, err := client.Do(req)
					if err == nil {
						coursesHTML, err := io.ReadAll(resp.Body)
						resp.Body.Close()
						if err == nil {
							courses := parseRegisteredCourses(string(coursesHTML))
							if len(courses) > 0 {
								return courses, nil
							}
						}
					}
				}
			}
		}
		clearCookies()
	}

	menuHTML, err := loginWithCredentials(client, userID, password)
	if err != nil {
		return nil, err
	}

	coursesURL := findRegisteredCoursesLink(menuHTML, imsBase)
	if coursesURL == "" {
		return nil, fmt.Errorf("registered courses link not found")
	}

	req, err := newRequest("GET", coursesURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Referer", imsBase+"student_login.php")
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	coursesHTML, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		return nil, err
	}

	courses := parseRegisteredCourses(string(coursesHTML))
	return courses, nil
}

func fetchTimetable(client *http.Client, userID, password string, todayOnly bool) ([]TimetableSlot, map[string][]TimetableSlot, error) {
	if loadErr := loadCookies(client); loadErr == nil {
		menuHTML, err := loginToIMS(client)
		if err == nil && menuHTML != "" {
			timetableURL := findTimetableLink(menuHTML, imsBase)
			if timetableURL != "" {
				coursesURL := findRegisteredCoursesLink(menuHTML, imsBase)
				var registered []RegisteredCourse
				if coursesURL != "" {
					req, err := newRequest("GET", coursesURL, nil)
					if err == nil {
						req.Header.Set("Referer", imsBase+"student_login.php")
						resp, err := client.Do(req)
						if err == nil {
							html, err := io.ReadAll(resp.Body)
							resp.Body.Close()
							if err == nil {
								registered = parseRegisteredCourses(string(html))
							}
						}
					}
				}

				req, err := newRequest("GET", timetableURL, nil)
				if err == nil {
					req.Header.Set("Referer", imsBase+"student_login.php")
					resp, err := client.Do(req)
					if err == nil {
						ttHTML, err := io.ReadAll(resp.Body)
						resp.Body.Close()
						if err == nil {
							todaySlots := parseTimetable(string(ttHTML), todayOnly, registered)
							weekSlots := parseTimetableWeek(string(ttHTML), registered)
							if len(todaySlots) > 0 || len(weekSlots) > 0 {
								return todaySlots, weekSlots, nil
							}
						}
					}
				}
			}
		}
		clearCookies()
	}

	menuHTML, err := loginWithCredentials(client, userID, password)
	if err != nil {
		return nil, nil, err
	}

	timetableURL := findTimetableLink(menuHTML, imsBase)
	if timetableURL == "" {
		return nil, nil, fmt.Errorf("timetable link not found")
	}

	coursesURL := findRegisteredCoursesLink(menuHTML, imsBase)
	var registered []RegisteredCourse
	if coursesURL != "" {
		req, err := newRequest("GET", coursesURL, nil)
		if err == nil {
			req.Header.Set("Referer", imsBase+"student_login.php")
			resp, err := client.Do(req)
			if err == nil {
				html, err := io.ReadAll(resp.Body)
				resp.Body.Close()
				if err == nil {
					registered = parseRegisteredCourses(string(html))
				}
			}
		}
	}

	req, err := newRequest("GET", timetableURL, nil)
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Referer", imsBase+"student_login.php")
	resp, err := client.Do(req)
	if err != nil {
		return nil, nil, err
	}
	ttHTML, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		return nil, nil, err
	}

	todaySlots := parseTimetable(string(ttHTML), todayOnly, registered)
	weekSlots := parseTimetableWeek(string(ttHTML), registered)
	return todaySlots, weekSlots, nil
}

type ExtendedResult struct {
	Status        string                       `json:"status"`
	ElapsedMs     int64                        `json:"elapsed_ms"`
	Attendance    map[string]map[string]int    `json:"attendance,omitempty"`
	Timeline      map[string][]map[string]string `json:"timeline,omitempty"`
	SubjectNames  map[string]string            `json:"subject_names,omitempty"`
	Courses       []RegisteredCourse           `json:"courses,omitempty"`
	TodayTimetable []TimetableSlot             `json:"timetable_today,omitempty"`
	WeekTimetable  map[string][]TimetableSlot  `json:"timetable_week,omitempty"`
}

func main() {
	if len(os.Args) < 3 {
		fmt.Fprintf(os.Stderr, "Usage: fast_scraper_go <user_id> <password> [year] [semester] [flags]\n")
		fmt.Fprintf(os.Stderr, "Flags: --clear-cookies, --courses, --timetable, --full, --json\n")
		os.Exit(1)
	}

	userID := os.Args[1]
	password := os.Args[2]
	year := ""
	semester := ""
	clearCookiesFlag := false
	concurrentFlag := false
	usersFile := ""
	coursesFlag := false
	timetableFlag := false
	fullFlag := false
	jsonFlag := false

	for i := 5; i < len(os.Args); i++ {
		switch os.Args[i] {
		case "--clear-cookies":
			clearCookiesFlag = true
		case "--concurrent":
			concurrentFlag = true
		case "--users":
			if i+1 < len(os.Args) {
				i++
				usersFile = os.Args[i]
			}
		case "--year":
			if i+1 < len(os.Args) {
				i++
				year = os.Args[i]
			}
		case "--semester":
			if i+1 < len(os.Args) {
				i++
				semester = os.Args[i]
			}
		case "--courses":
			coursesFlag = true
		case "--timetable":
			timetableFlag = true
		case "--full":
			fullFlag = true
		case "--json":
			jsonFlag = true
		}
	}

	if len(os.Args) > 3 {
		year = os.Args[3]
	}
	if len(os.Args) > 4 {
		semester = os.Args[4]
	}

	if clearCookiesFlag {
		clearCookies()
		fmt.Println("Cached cookies cleared.")
		return
	}

	if concurrentFlag && usersFile != "" {
		data, err := os.ReadFile(usersFile)
		if err != nil {
			log.Fatalf("Failed to read users file: %v", err)
		}
		lines := strings.Split(string(data), "\n")
		var userIDs []string
		for _, line := range lines {
			line = strings.TrimSpace(line)
			if line != "" {
				userIDs = append(userIDs, line)
			}
		}
		if len(userIDs) == 0 {
			log.Fatal("No users found in file")
		}

		fmt.Printf("Running Go concurrent scraper for %d users...\n", len(userIDs))
		start := time.Now()
		results := fetchAttendanceConcurrent(userIDs, password, year, semester, 3)
		totalElapsed := time.Since(start).Milliseconds()

		fmt.Printf("\n=== Concurrent Results ===\n")
		fmt.Printf("Total time: %dms\n", totalElapsed)
		fmt.Printf("Users: %d\n", len(results))
		for _, r := range results {
			fmt.Printf("  %s: status=%s, elapsed=%dms, subjects=%d\n",
				r.UserID, r.Result.Status, r.Result.ElapsedMs, len(r.Result.Data))
		}
		return
	}

	jar, _ := cookiejar.New(nil)
	transport := &http.Transport{
		MaxIdleConns:        10,
		MaxIdleConnsPerHost: 5,
		IdleConnTimeout:     90 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
	}

	client := &http.Client{
		Jar:       jar,
		Timeout:   httpTimeout,
		Transport: transport,
	}

	start := time.Now()
	ext := ExtendedResult{Status: "success"}

	if fullFlag || coursesFlag || timetableFlag {
		menuHTML, loginErr := loginWithCredentials(client, userID, password)
		if loginErr != nil {
			ext.Status = "login_failed"
			if jsonFlag {
				ext.ElapsedMs = time.Since(start).Milliseconds()
				data, _ := json.Marshal(ext)
				fmt.Println(string(data))
			} else {
				fmt.Printf("Login failed: %v\n", loginErr)
			}
			return
		}

		if fullFlag || coursesFlag {
			coursesURL := findRegisteredCoursesLink(menuHTML, imsBase)
			if coursesURL != "" {
				req, err := newRequest("GET", coursesURL, nil)
				if err == nil {
					req.Header.Set("Referer", imsBase+"student_login.php")
					resp, err := client.Do(req)
					if err == nil {
						html, err := io.ReadAll(resp.Body)
						resp.Body.Close()
						if err == nil {
							ext.Courses = parseRegisteredCourses(string(html))
						}
					}
				}
			}
		}

		if fullFlag || timetableFlag {
			timetableURL := findTimetableLink(menuHTML, imsBase)
			if timetableURL != "" {
				var registered []RegisteredCourse
				coursesURL := findRegisteredCoursesLink(menuHTML, imsBase)
				if coursesURL != "" {
					req, err := newRequest("GET", coursesURL, nil)
					if err == nil {
						req.Header.Set("Referer", imsBase+"student_login.php")
						resp, err := client.Do(req)
						if err == nil {
							html, err := io.ReadAll(resp.Body)
							resp.Body.Close()
							if err == nil {
								registered = parseRegisteredCourses(string(html))
							}
						}
					}
				}

				req, err := newRequest("GET", timetableURL, nil)
				if err == nil {
					req.Header.Set("Referer", imsBase+"student_login.php")
					resp, err := client.Do(req)
					if err == nil {
						ttHTML, err := io.ReadAll(resp.Body)
						resp.Body.Close()
						if err == nil {
							ext.TodayTimetable = parseTimetable(string(ttHTML), true, registered)
							ext.WeekTimetable = parseTimetableWeek(string(ttHTML), registered)
						}
					}
				}
			}
		}

		if fullFlag {
			attendanceRe := regexp.MustCompile(`href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Attendance<`)
			attendanceMatch := attendanceRe.FindStringSubmatch(menuHTML)
			if len(attendanceMatch) >= 2 {
				attendanceURL := attendanceMatch[1]
				req, err := newRequest("GET", attendanceURL, nil)
				if err == nil {
					req.Header.Set("Referer", imsBase+"student_login.php")
					resp, err := client.Do(req)
					if err == nil {
						attendanceHTML, err := io.ReadAll(resp.Body)
						resp.Body.Close()
						if err == nil {
							attendanceStr := string(attendanceHTML)

							initialYear := extractSelectValue(attendanceStr, "year")
							initialSem := extractSelectValue(attendanceStr, "sem")
							resolvedYear := year
							if resolvedYear == "" {
								resolvedYear = initialYear
							}
							resolvedSemester := semester
							if resolvedSemester == "" {
								resolvedSemester = initialSem
							}

							encYearRe2 := regexp.MustCompile(`name='enc_year' id='enc_year' value='([^']+)'`)
							encSemRe2 := regexp.MustCompile(`name='enc_sem' id='enc_sem' value='([^']+)'`)
							recentityRe2 := regexp.MustCompile(`name=recentitycode value='([^']+)'`)
							deptRe2 := regexp.MustCompile(`name=dept value='([^']+)'`)
							degreeRe2 := regexp.MustCompile(`name=degree value='([^']+)'`)

							encYear := encYearRe2.FindStringSubmatch(attendanceStr)
							encSem := encSemRe2.FindStringSubmatch(attendanceStr)
							recentity := recentityRe2.FindStringSubmatch(attendanceStr)
							dept := deptRe2.FindStringSubmatch(attendanceStr)
							degree := degreeRe2.FindStringSubmatch(attendanceStr)

							if len(encYear) >= 2 && len(encSem) >= 2 && len(recentity) >= 2 && len(dept) >= 2 && len(degree) >= 2 {
								attData := url.Values{}
								attData.Set("year", resolvedYear)
								attData.Set("enc_year", encYear[1])
								attData.Set("sem", resolvedSemester)
								attData.Set("enc_sem", encSem[1])
								attData.Set("submit", "Submit")
								attData.Set("recentitycode", recentity[1])
								attData.Set("dept", dept[1])
								attData.Set("degree", degree[1])
								attData.Set("ename", "")
								attData.Set("ecode", "")

								req, err = newRequest("POST", attendanceURL, strings.NewReader(attData.Encode()))
								if err == nil {
									req.Header.Set("Referer", attendanceURL)
									req.Header.Set("Origin", "https://www.imsnsit.org")
									req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
									req.Header.Set("Upgrade-Insecure-Requests", "1")
									resp, err = client.Do(req)
									if err == nil {
										resultHTML, err := io.ReadAll(resp.Body)
										resp.Body.Close()
										if err == nil {
											ext.Attendance = parseAttendanceTable(string(resultHTML))
										}
									}
								}
							}
						}
					}
				}
			}
		}
	} else {
		result := fetchAttendance(client, userID, password, year, semester)
		ext.Attendance = result.Data
		ext.Timeline = result.Timeline
		ext.SubjectNames = result.SubjectNames
		ext.Status = result.Status
	}

	ext.ElapsedMs = time.Since(start).Milliseconds()

	if jsonFlag {
		data, _ := json.Marshal(ext)
		fmt.Println(string(data))
		return
	}

	fmt.Printf("\n=== Results ===\n")
	fmt.Printf("Status:    %s\n", ext.Status)
	fmt.Printf("Elapsed:   %dms\n", ext.ElapsedMs)

	if len(ext.Courses) > 0 {
		fmt.Printf("\n--- Registered Courses ---\n")
		for _, c := range ext.Courses {
			groupStr := c.Group
			batchStr := c.Batch
			extra := ""
			if batchStr != "" {
				extra = ", batch=" + batchStr
			}
			fmt.Printf("  %s (%s): section=%s%s group=%s\n", c.Code, c.Name, c.Section, extra, groupStr)
		}
	}

	if len(ext.TodayTimetable) > 0 {
		fmt.Printf("\n--- Today's Timetable ---\n")
		for _, s := range ext.TodayTimetable {
			fmt.Printf("  %s: %s\n", s.Time, s.Subject)
		}
	}

	if len(ext.WeekTimetable) > 0 {
		fmt.Printf("\n--- Weekly Timetable ---\n")
		for day, slots := range ext.WeekTimetable {
			fmt.Printf("  %s:\n", day)
			for _, s := range slots {
				fmt.Printf("    %s: %s\n", s.Time, s.Subject)
			}
		}
	}

	if len(ext.Attendance) > 0 {
		fmt.Printf("\n--- Attendance ---\n")
		for code, entry := range ext.Attendance {
			name := ext.SubjectNames[code]
			nameStr := ""
			if name != "" {
				nameStr = " (" + name + ")"
			}
			fmt.Printf("  %s%s: present=%d, absent=%d, total=%d\n",
				code, nameStr, entry["present"], entry["absent"], entry["total"])
		}
	}
}
