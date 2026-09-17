# Thorium Reader Setup Guide

This guide walks you through connecting [Thorium Reader](https://www.edrlab.org/software/thorium-reader/) to your library catalog so you can browse, borrow, and read publications.

## What You Need

Before you start, ask your library administrator for:

- Your **username** and **password**
- The **catalog URL** (looks like `https://catalog.example.com/opds/v2/library/`)
- Your **LCP passphrase** setup instructions (needed to open borrowed books)

---

## Step 1: Install Thorium Reader

1. Go to [thorium-reader.org](https://www.edrlab.org/software/thorium-reader/)
2. Download the version for your computer (Windows, macOS, or Linux)
3. Install it like any other application
4. Open Thorium Reader

---

## Step 2: Set Up Your LCP Passphrase

Before you can borrow books, you need an **LCP passphrase** on your account. This passphrase protects borrowed content -- think of it as a second password specifically for opening books.

**Choose something you will remember.** You will need to type it in Thorium the first time you open a borrowed book.

**How to set it:** Ask your administrator for instructions. Depending on your organization, you may be able to set it through:

- A web portal or profile settings page
- A link or form provided by your administrator

!!! note
    If you forget your passphrase, you will need to set a new one and re-borrow any books you had checked out.

---

## Step 3: Add the Catalog

1. Open Thorium Reader
2. Click **Catalogs** in the left sidebar (the icon looks like a book with a plus sign)
3. Click **Add an OPDS feed**
4. Fill in the fields:

    | Field | What to enter |
    |-------|---------------|
    | **Name** | A name you will recognize (e.g., `My Library`) |
    | **URL** | The catalog URL from your administrator |

5. Click **Add**

!!! tip
    The URL usually looks like this: `https://catalog.example.com/opds/v2/library/`

    Make sure to include the **trailing slash** (`/`) at the end of the URL.

---

## Step 4: Sign In

When you open the catalog for the first time, Thorium will show a login screen.

1. Enter your **username** (or email)
2. Enter your **password**
3. Click **Sign In**

That's it -- Thorium remembers your credentials for future visits.

!!! note
    Some catalogs are public and let you browse without signing in. You will still need to sign in when you want to borrow a book.

---

## Step 5: Browse the Catalog

After signing in, you will see the catalog organized into sections:

- **All Publications** -- Everything available in the catalog
- **New Publications** -- Recently added books
- **Popular** -- Most-read books
- **My Shelf** -- Books you currently have borrowed
- Additional collections created by your library

Each publication shows its title, author, cover image, and a short description.

### Searching

Use the **search bar** in Thorium to find books by title, author, publisher, or subject.

---

## Step 6: Borrow a Book

1. Find a book you want to read
2. Click the **Borrow** button

If the book is available:

- Thorium downloads it automatically
- The book appears in your **My Shelf** section and in your Thorium library

If the book shows **Unavailable**, all copies are currently borrowed by other readers. Check back later.

!!! info "Loan duration"
    Loans typically last **14 days** (your administrator may have set a different period). After the loan expires, the book will no longer open. You can borrow it again if you need more time.

---

## Step 7: Read the Book

1. Go to your library in Thorium (click **My Books** in the left sidebar)
2. Find the borrowed book
3. Click to open it
4. **The first time you open a borrowed book**, Thorium will ask for your **LCP passphrase**
5. Type the passphrase you set up in Step 2 and click **Submit**

Thorium unlocks the book and you can start reading. You only need to enter the passphrase once -- Thorium remembers it for future books.

---

## Step 8: Return a Book

When you finish reading, return the book so others can borrow it:

1. Go to **Catalogs** in the left sidebar
2. Open your catalog
3. Go to **My Shelf**
4. Click **Return** next to the book

!!! tip
    If you forget to return a book, don't worry. The loan expires automatically after the loan period ends.

---

## Troubleshooting

### I can't connect to the catalog

- Double-check the URL -- make sure it ends with a `/`
- Make sure your computer is connected to the internet
- If the URL starts with `https://` and you get a security warning, contact your administrator

### No books appear in the catalog

- Make sure you are signed in (try signing out and back in)
- The catalog may not have any books yet -- contact your administrator

### "LCP passphrase not configured" when borrowing

You haven't set your LCP passphrase yet. Go back to [Step 2](#step-2-set-up-your-lcp-passphrase) and follow the instructions.

### "Incorrect passphrase" when opening a book

The passphrase you typed does not match the one on your account. Try again carefully. If you can't remember it, you will need to set a new one (ask your administrator) and re-borrow the book.

### A book won't open after the loan expired

This is expected. Once a loan expires, the book is locked. Simply borrow it again.

### The Borrow button says "Unavailable"

All copies are currently checked out by other readers. Wait for someone to return their copy and try again later.

---

## Quick Reference

| Action | Where in Thorium |
|--------|-----------------|
| Add a catalog | Catalogs > Add an OPDS feed |
| Browse books | Catalogs > (your catalog name) |
| Search | Search bar within the catalog view |
| See your borrowed books | Catalogs > (your catalog) > My Shelf |
| Read a book | My Books > click the book |
| Return a book | Catalogs > (your catalog) > My Shelf > Return |