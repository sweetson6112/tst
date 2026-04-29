import streamlit as st
import pdfplumber
import tempfile
import os
import json

# -----------------------------
# PDF TEXT EXTRACTION FUNCTION
# -----------------------------
def extract_text(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
    return text

# -----------------------------
# PARSER FUNCTION (CUSTOMIZE THIS)
# -----------------------------
def parse_ooc_pdf(pdf_path):
    text = extract_text(pdf_path)

    # Example parsing (customize as needed)
    data = {}

    lines = text.split("\n")

    for line in lines:
        if "OOC No" in line:
            data["OOC No"] = line.split(":")[-1].strip()
        elif "IEC" in line:
            data["IEC"] = line.split(":")[-1].strip()
        elif "Name" in line:
            data["Name"] = line.split(":")[-1].strip()

    data["raw_text_preview"] = text[:500]

    return data

# -----------------------------
# STREAMLIT UI
# -----------------------------
st.title("OOC PDF Parser")

uploaded_file = st.file_uploader("Upload OOC PDF", type="pdf")

if uploaded_file is not None:
    # Save to temp file (required for cloud)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(uploaded_file.read())
        pdf_path = tmp.name

    # Debug check
    st.write("Processing file...")

    if os.path.exists(pdf_path):
        data = parse_ooc_pdf(pdf_path)

        st.subheader("Extracted Data")
        st.json(data)

        # Download option
        st.download_button(
            label="Download JSON",
            data=json.dumps(data, indent=2),
            file_name="ooc_output.json",
            mime="application/json"
        )
    else:
        st.error("File not found after upload.")

# -----------------------------
# REQUIREMENTS (for reference)
# -----------------------------
# streamlit
# pdfplumber
