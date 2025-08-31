# autoky
Use an LLM to create keywords that describe images. View those images, and filter by keyword.

This script can be used in two ways. First, to create a list of keywords that describe
images, along with a RANK and an SHA-256 hash code. For example:

C:\Files\image.png,ancient,art,bust,classical,detail,expression,face,head,historical,light,male,marble,monochrome,neutral,
photo,portrait,RANK 8,Roman,sculpture,shadow,stone,texture,08012ea05a64aee1d59d415cca109fe6d1f933412cbaf15a1c55b8b65fd5181b

Second, if you provide a text file with one of those entries per line, the script displays a UI that lets you
filter the images by keywords or RANK, and display them.

To run the script in the first mode, provide a list of files or folders as arguments on the command line.
Any folders will be recursively examined for images. Wildcards are acceptable. The supported image types
are: jpg, jpeg, png, gif, webbp, bmp, tif, and tiff.

Before running the app, you should have a vision-capable LLM running on http://localhost:11434. The script
was developed and tested using gemma3:12b on ollama.

The keywords are displayed on stdout. Just redirect to a file if you want to use them later, or for viewing.

Filenames in the CSV are absolute, so the CSV doesn't have to be in the same folder as the images in order to view the images.

For viewing, invoke the script with one or more txt files containing CSV keywords in the format above, one image per line.
The script will identify and not display duplicates, based on the SHA-256 hash code. The UI shows how many duplicates were
identified.

In the UI, you can specify multiple keywords by separating them with commas.

You may want to modify the prompt that tells the LLM how to generate keywords and ranks, and/or the URL to access the LLM
and the model used. All of those parameters are located near the beginning of the file.

NOTE: this code was AI-generated. I provided the prompts to generate the code and the keywords, but not the coding style.
