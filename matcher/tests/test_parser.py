from sww.jobs.parser import JOBS_URL, login_required, parse_detail, parse_listing, safe_job_url


def test_freeform_location_does_not_absorb_following_description():
    prose = "Company background and responsibilities. " * 100
    html = f'''<div class="modal is--visible"><h2>Posting 123</h2>
      <div class="is--long-form-reading"><p><strong>Location:</strong></p>
      <p>Toronto, ON</p><p>{prose}</p></div></div>'''
    detail = parse_detail(html, "123")
    assert detail["location"] == "Toronto, ON"
    assert prose.strip() in detail["description"]


def test_structured_location_wins_over_embedded_location_label():
    html = '''<div class="modal is--visible"><h2>Posting 123</h2>
      <dl><dt>Job Location</dt><dd>Waterloo, ON</dd></dl>
      <div class="is--long-form-reading"><strong>Location:</strong>Toronto<br>
      <p>Other office information and responsibilities.</p></div></div>'''
    detail = parse_detail(html, "123")
    assert detail["location"] == "Waterloo, ON"
    assert "Other office information" in detail["description"]


def test_modern_table_uses_reordered_headers_and_handles_pagination():
    html = '''<p>2 results 1 - 1</p><table><thead><tr>
      <th></th><th>City</th><th>Organization</th><th>Job Title <span class="material-icons">sort</span></th><th>Application Deadline</th>
      </tr></thead><tbody><tr><td><input name="dataViewerSelection" value="123"></td>
      <td>Toronto</td><td>Example Corp</td><td><a class="overflow--ellipsis" href="javascript:void(0)">Python Engineer</a></td>
      <td>Sep 20, 2026</td></tr></tbody></table>
      <a class="pagination__link active" href="#">1</a><a class="pagination__link" href="#">2</a>'''
    result = parse_listing(html)
    assert result.expected_total == 2
    assert result.current_page == 1
    assert result.next_page and not result.end_confirmed
    assert result.jobs[0]["company"] == "Example Corp"
    assert result.jobs[0]["location"] == "Toronto"
    assert result.jobs[0]["title"] == "Python Engineer"
    assert result.jobs[0]["deadline"] == "Sep 20, 2026"
    assert "123" in result.targets


def test_legacy_direct_posting_links_and_dedupe():
    row = '''<tr><td>9</td><td><a href="?action=displayJob&amp;jobId=9">QA Intern</a></td><td>Example</td></tr>'''
    result = parse_listing('<p>1 results</p><table><tr><th>ID</th><th>Job Title</th><th>Employer</th></tr>' + row + row + '</table>')
    assert len(result.jobs) == 1
    assert result.jobs[0]["id"] == "9"
    assert result.targets["9"].url == JOBS_URL + "?action=displayJob&jobId=9"
    assert result.end_confirmed


def test_card_view_uses_structured_labels():
    result = parse_listing('''<p>1 results</p><div class="doc-viewer__card">
       <input name="dataViewerSelection" value="123"><a class="overflow--ellipsis" href="#">Data Intern</a>
       <dl><dt>Organization</dt><dd>Example</dd><dt>City</dt><dd>Ottawa</dd></dl></div>''')
    assert result.jobs[0]["company"] == "Example"
    assert result.jobs[0]["location"] == "Ottawa"


def test_no_results_is_distinct_from_selector_mismatch():
    assert parse_listing("<main>No results found</main>").empty
    assert parse_listing("<main>0 results</main>").empty
    mismatch = parse_listing("<main><h1>Job Board</h1><div>Loading jobs</div></main>")
    assert not mismatch.empty and not mismatch.recognized and not mismatch.end_confirmed


def test_stale_empty_notice_and_action_icons_do_not_hide_job_title():
    result = parse_listing('''<span hidden>0 results</span><p>1 results</p><table>
      <tr><th>Actions</th><th>ID</th><th>Job Title</th><th>Organization</th></tr>
      <tr><td><a href="?action=displayJob&amp;jobId=999">Print</a></td><td>488595</td>
      <td><a href="#">Junior Software Developer</a></td><td>Example Inc</td></tr></table>''')
    assert not result.empty
    assert result.expected_total == 1
    assert result.jobs[0]["id"] == "488595"
    assert result.jobs[0]["title"] == "Junior Software Developer"
    assert result.targets["488595"].url is None


def test_standalone_overview_with_badge_and_stacked_labels():
    html = '''<body><aside>Account private@example.org</aside><main>
      <header><span>488595</span><h2>Junior Software Developer</h2></header>
      <h3>JOB POSTING INFORMATION</h3>
      <section><div><strong>Work Term:</strong></div><div>2027 - Winter</div></section>
      <section><b>Job Title:</b><p>Junior Software Developer</p></section>
      <section><b>Number of Job Openings:</b><p>2</p></section>
      <section><b>Level:</b><ul><li>Junior</li><li>Intermediate</li></ul></section>
      <section><b>Job Summary:</b><div><p>Build Python services.</p><p>Maintain SQL pipelines.</p></div></section>
      <section><b>Required Skills:</b><ul><li>Python</li><li>SQL</li></ul></section>
      <section><b>Application Deadline:</b><p>September 16, 2026</p></section>
      </main></body>'''
    detail = parse_detail(html, "488595")
    assert detail["title"] == "Junior Software Developer"
    assert detail["description"] == "Build Python services.\nMaintain SQL pipelines."
    assert detail["requirements"] == "Python\nSQL"
    assert detail["metadata"]["Work Term"] == "2027 - Winter"
    assert detail["metadata"]["Number of Job Openings"] == "2"
    assert detail["metadata"]["Level"] == "Junior\nIntermediate"
    assert "2027 - Winter" in detail["metadata"]["detail_text"]
    assert detail["deadline"] == "September 16, 2026"
    assert "private@example.org" not in str(detail)
    assert parse_detail(html, "488545") is None


def test_unknown_pagination_does_not_claim_complete():
    result = parse_listing('''<table><tr><th>Job Title</th></tr><tr><td>
       <a href="?action=displayJob&amp;jobId=9">QA Intern</a></td></tr></table>''')
    assert len(result.jobs) == 1
    assert not result.end_confirmed


def test_disabled_next_proves_last_page():
    result = parse_listing('<nav class="pagination"><button aria-label="Next page" disabled>Next</button></nav>')
    assert result.end_confirmed and result.next_page is None


def test_collapsed_pagination_recognizes_previous_without_page_one():
    result = parse_listing('''<p>812 results 801 - 812</p><nav class="pagination">
      <button aria-label="Previous page">chevron_left</button>
      <a href="#">16</a><a href="#" aria-current="page">17</a>
      <button aria-label="Next page" disabled>chevron_right</button></nav>''')
    assert result.current_page == 17 and not result.start_confirmed
    assert result.previous_page and result.first_page is None
    assert result.end_confirmed


def test_first_page_icon_and_disabled_previous_boundary():
    result = parse_listing('<button class="pagination__link" aria-label="First page">first_page</button>')
    assert result.first_page
    result = parse_listing('<button class="pagination__link" aria-label="Previous page" disabled>chevron_left</button>')
    assert result.start_confirmed and result.previous_page is None


def test_previous_numeric_page_is_a_fallback():
    result = parse_listing('<nav class="pagination"><a href="#">16</a><a class="active" href="#">17</a></nav>')
    assert result.previous_page and result.first_page is None


def test_modal_requires_expected_id_and_extracts_requirements():
    html = '''<div class="modal is--visible"><h2>Posting 123</h2><div class="is--long-form-reading">
       <table><tr><td>Job Summary:</td><td>Build reliable services</td></tr>
       <tr><td>Required Skills:</td><td>Python, SQL</td></tr></table></div></div>'''
    detail = parse_detail(html, "123")
    assert detail["description"] == "Build reliable services"
    assert detail["requirements"] == "Python, SQL"
    assert detail["metadata"]["detail_status"] == "complete"
    assert parse_detail(html, "12") is None
    assert parse_detail(html.replace("is--visible", "is--hidden"), "123") is None


def test_detail_does_not_collect_surrounding_account_data():
    html = '''<body>Account: private@example.org<form><input name="csrf" value="SECRET"></form>
      <div class="modal is--visible"><h2>Posting 123</h2><div class="is--long-form-reading">Build software using Python</div></div></body>'''
    result = parse_detail(html, "123")
    assert "private@example.org" not in str(result)
    assert "SECRET" not in str(result)


def test_navigation_rejects_external_write_auth_and_script_links():
    for url in ["https://evil.test/myAccount/co-op/full/jobs.htm", "?action=apply&jobId=1", "?jobId=1&token=SECRET", "javascript:fetch('/apply')", "https://waterlooworks.uwaterloo.ca:evil/myAccount/jobs.htm", "https://waterlooworks.uwaterloo.ca/myAccount/logout.htm"]:
        assert safe_job_url(url) is None
    assert safe_job_url("?action=displayJob&jobId=123")
    result = parse_listing('''<div class="doc-viewer__card"><input name="dataViewerSelection" value="123">
      <a class="overflow--ellipsis" href="javascript:apply(123)">Do not click</a></div>''')
    assert "123" not in result.targets


def test_login_expiry_and_sso_are_detected_without_credentials():
    assert login_required('<input type="password">', JOBS_URL)
    assert login_required("Session has expired", JOBS_URL)
    assert login_required("Sign in", "https://login.microsoftonline.com/example")
    assert login_required("WaterlooWorks - Not Logged In", "https://waterlooworks.uwaterloo.ca/notLoggedIn.htm")
    assert login_required("", "https://waterlooworks.uwaterloo.ca/notLoggedIn.htm")
    assert not login_required("Job qualifications: login systems experience", JOBS_URL)
