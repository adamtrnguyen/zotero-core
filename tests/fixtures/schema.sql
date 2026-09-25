-- A subset of Zotero 9.0.6's schema, transcribed VERBATIM from the live database
-- (`SELECT sql FROM sqlite_master`) on 2026-08-13.
--
-- Verbatim matters. The gates read `deletedItems` for trash state, `itemData` +
-- `itemDataValues` + `fields` for titles, and three separate parent columns
-- (`itemAttachments`, `itemNotes`, `itemAnnotations`) for lineage. A hand-simplified
-- fixture -- say, a `title` column on `items`, or a `deleted` flag instead of the
-- side table -- would let SQL that cannot run against the real library pass here.
-- Zotero does not store a title on `items` and does not carry a `deleted` column;
-- the trash is a row's PRESENCE in `deletedItems`.
--
-- Only the CREATE TABLEs are copied. The real schema's triggers reference tables
-- this subset omits (`fieldsCombined`, `charsets`), and they enforce nothing the
-- gates depend on.

CREATE TABLE libraries (    libraryID INTEGER PRIMARY KEY,    type TEXT NOT NULL,    editable INT NOT NULL,    filesEditable INT NOT NULL,    version INT NOT NULL DEFAULT 0,    storageVersion INT NOT NULL DEFAULT 0,    lastSync INT NOT NULL DEFAULT 0,    archived INT NOT NULL DEFAULT 0, isAdmin INT NOT NULL DEFAULT 0);

CREATE TABLE itemTypes (    itemTypeID INTEGER PRIMARY KEY,    typeName TEXT,    templateItemTypeID INT,    display INT DEFAULT 1 );

CREATE TABLE fields (    fieldID INTEGER PRIMARY KEY,    fieldName TEXT,    fieldFormatID INT);

CREATE TABLE items (    itemID INTEGER PRIMARY KEY,    itemTypeID INT NOT NULL,    dateAdded TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,    dateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,    clientDateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,    libraryID INT NOT NULL,    key TEXT NOT NULL,    version INT NOT NULL DEFAULT 0,    synced INT NOT NULL DEFAULT 0,    UNIQUE (libraryID, key));

CREATE TABLE itemDataValues (    valueID INTEGER PRIMARY KEY,    value UNIQUE);

CREATE TABLE itemData (    itemID INT,    fieldID INT,    valueID,    PRIMARY KEY (itemID, fieldID));

CREATE TABLE itemAttachments (    itemID INTEGER PRIMARY KEY,    parentItemID INT,    linkMode INT,    contentType TEXT,    charsetID INT,    path TEXT,    syncState INT DEFAULT 0,    storageModTime INT,    storageHash TEXT,    lastProcessedModificationTime INT, lastRead INT);

CREATE TABLE itemNotes (    itemID INTEGER PRIMARY KEY,    parentItemID INT,    note TEXT,    title TEXT);

CREATE TABLE itemAnnotations (    itemID INTEGER PRIMARY KEY,    parentItemID INT NOT NULL,    type INTEGER NOT NULL,    authorName TEXT,    text TEXT,    comment TEXT,    color TEXT,    pageLabel TEXT,    sortIndex TEXT NOT NULL,    position TEXT NOT NULL,    isExternal INT NOT NULL);

CREATE TABLE collections (    collectionID INTEGER PRIMARY KEY,    collectionName TEXT NOT NULL,    parentCollectionID INT DEFAULT NULL,    clientDateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,    libraryID INT NOT NULL,    key TEXT NOT NULL,    version INT NOT NULL DEFAULT 0,    synced INT NOT NULL DEFAULT 0,    UNIQUE (libraryID, key));

CREATE TABLE collectionItems (    collectionID INT NOT NULL,    itemID INT NOT NULL,    orderIndex INT NOT NULL DEFAULT 0,    PRIMARY KEY (collectionID, itemID));

CREATE TABLE tags (    tagID INTEGER PRIMARY KEY,    name TEXT NOT NULL UNIQUE);

CREATE TABLE itemTags (    itemID INT NOT NULL,    tagID INT NOT NULL,    type INT NOT NULL,    PRIMARY KEY (itemID, tagID));

CREATE TABLE creators (    creatorID INTEGER PRIMARY KEY,    firstName TEXT,    lastName TEXT,    fieldMode INT,    UNIQUE (lastName, firstName, fieldMode));

CREATE TABLE creatorTypes (    creatorTypeID INTEGER PRIMARY KEY,    creatorType TEXT);

CREATE TABLE itemCreators (    itemID INT NOT NULL,    creatorID INT NOT NULL,    creatorTypeID INT NOT NULL DEFAULT 1,    orderIndex INT NOT NULL DEFAULT 0,    PRIMARY KEY (itemID, creatorID, creatorTypeID, orderIndex),    UNIQUE (itemID, orderIndex));

CREATE TABLE deletedItems (    itemID INTEGER PRIMARY KEY,    dateDeleted DEFAULT CURRENT_TIMESTAMP NOT NULL);

CREATE TABLE baseFieldMappingsCombined (    itemTypeID INT,    baseFieldID INT,    fieldID INT,    PRIMARY KEY (itemTypeID, baseFieldID, fieldID));

-- Seeded exactly as the live database has them: libraryID 1 is type 'user', which
-- is the library `bootstrap.js` resolves keys against via
-- `Zotero.Libraries.userLibraryID`. The six group libraries the real database also
-- carries are represented by ONE here, so the "a group-library key must not resolve"
-- test has something to point at.
INSERT INTO libraries (libraryID, type, editable, filesEditable) VALUES (1, 'user', 1, 1);
INSERT INTO libraries (libraryID, type, editable, filesEditable) VALUES (2, 'group', 1, 1);

-- Real itemTypeIDs from the live database. `bookSection` and `conferencePaper` are here
-- because they are two of the ten types that map `publicationTitle` onto a field of
-- their own -- the remap that made a successful venue write read as unverified.
INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'annotation');
INSERT INTO itemTypes (itemTypeID, typeName) VALUES (3, 'attachment');
INSERT INTO itemTypes (itemTypeID, typeName) VALUES (7, 'book');
INSERT INTO itemTypes (itemTypeID, typeName) VALUES (8, 'bookSection');
INSERT INTO itemTypes (itemTypeID, typeName) VALUES (11, 'conferencePaper');
INSERT INTO itemTypes (itemTypeID, typeName) VALUES (22, 'journalArticle');
INSERT INTO itemTypes (itemTypeID, typeName) VALUES (28, 'note');
INSERT INTO itemTypes (itemTypeID, typeName) VALUES (31, 'preprint');

-- Real fieldIDs from the live database. The reads join on fieldName rather than the
-- number, so what these rows buy is that a query written against the real numbers
-- would also work here -- and that the duplicate check's 'DOI'/'ISBN'/'extra' names
-- resolve at all. The builder inserts any other field name on demand.
INSERT INTO fields (fieldID, fieldName) VALUES (1, 'title');
INSERT INTO fields (fieldID, fieldName) VALUES (2, 'abstractNote');
INSERT INTO fields (fieldID, fieldName) VALUES (6, 'date');
INSERT INTO fields (fieldID, fieldName) VALUES (7, 'language');
INSERT INTO fields (fieldID, fieldName) VALUES (8, 'shortTitle');
INSERT INTO fields (fieldID, fieldName) VALUES (13, 'url');
INSERT INTO fields (fieldID, fieldName) VALUES (16, 'extra');
INSERT INTO fields (fieldID, fieldName) VALUES (23, 'publisher');
INSERT INTO fields (fieldID, fieldName) VALUES (25, 'ISBN');
INSERT INTO fields (fieldID, fieldName) VALUES (59, 'DOI');
-- The base field and the two type-specific fields it maps to, plus the second mapping
-- bookSection carries. Real fieldIDs, so the join below is the real join.
INSERT INTO fields (fieldID, fieldName) VALUES (4, 'medium');
INSERT INTO fields (fieldID, fieldName) VALUES (38, 'publicationTitle');
INSERT INTO fields (fieldID, fieldName) VALUES (45, 'bookTitle');
INSERT INTO fields (fieldID, fieldName) VALUES (57, 'proceedingsTitle');
INSERT INTO fields (fieldID, fieldName) VALUES (63, 'format');

-- baseFieldMappingsCombined rows copied VERBATIM for these two types
-- (`SELECT itemTypeID, baseFieldID, fieldID ...` on 2026-08-13), IDs and all:
--   8 (bookSection)      medium(4) -> format(63),  publicationTitle(38) -> bookTitle(45)
--   11 (conferencePaper) publicationTitle(38) -> proceedingsTitle(57)
-- Real IDs rather than invented ones for the reason the header gives: a query written
-- against the live numbers has to work here too, and a hand-picked mapping would let a
-- resolver that reads the wrong column pass.
INSERT INTO baseFieldMappingsCombined (itemTypeID, baseFieldID, fieldID) VALUES (8, 4, 63);
INSERT INTO baseFieldMappingsCombined (itemTypeID, baseFieldID, fieldID) VALUES (8, 38, 45);
INSERT INTO baseFieldMappingsCombined (itemTypeID, baseFieldID, fieldID) VALUES (11, 38, 57);

-- creatorTypes: real IDs, and 1 is 'author' which is itemCreators' column default.
INSERT INTO creatorTypes (creatorTypeID, creatorType) VALUES (1, 'author');
INSERT INTO creatorTypes (creatorTypeID, creatorType) VALUES (2, 'contributor');
INSERT INTO creatorTypes (creatorTypeID, creatorType) VALUES (3, 'editor');

-- Zotero's fulltext index. Word -> item, with NO positions and NO counts: it can say
-- which documents contain a word and nothing about adjacency, which is why
-- `read/search.py` uses it only to narrow candidates before reading .zotero-ft-cache.
CREATE TABLE fulltextItems (    itemID INTEGER PRIMARY KEY,    indexedPages INT,    totalPages INT,    indexedChars INT,    totalChars INT,    version INT NOT NULL DEFAULT 0,    synced INT NOT NULL DEFAULT 0);

CREATE TABLE fulltextWords (    wordID INTEGER PRIMARY KEY,    word TEXT UNIQUE);

CREATE TABLE fulltextItemWords (    wordID INT,    itemID INT,    PRIMARY KEY (wordID, itemID));

-- Group libraries. `libraries` alone gives the id and type; the human-readable NAME
-- lives here, which is what makes "you are looking in the wrong library" answerable.
CREATE TABLE groups (    groupID INTEGER PRIMARY KEY,    libraryID INT NOT NULL UNIQUE,    name TEXT NOT NULL,    description TEXT NOT NULL DEFAULT '',    version INT NOT NULL DEFAULT 0);
