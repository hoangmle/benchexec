import Utils from "../utils/utils";

//Example data set to test the filtering by regex
const rows = [
  {
    test: {
      formatted: "10.5",
      original: "10.5"
    }
  },
  {
    test: {
      formatted: "10",
      original: "10"
    }
  },
  {
    test: {
      formatted: "9.3",
      original: "9.3"
    }
  },
  {
    test: {
      formatted: "11",
      original: "11"
    }
  },
  {
    test: {
      formatted: "11.001",
      original: "11.001"
    }
  }
];

//Function to test filtering by regex for data set 'rows' (return number of truely returnd values)
const getFilteredData = regex =>
  rows.filter(row => Utils.filterByRegex({ id: "test", value: regex }, row));

test("filterByRegex single entry without result", () => {
  expect(
    Utils.filterByRegex(
      {
        id: "test",
        value: "10:"
      },
      {
        test: {
          formatted: "7",
          original: "7"
        }
      }
    )
  ).toBe(false);
});

//use function getFilteredData to generate test cases with data set 'rows'

test("filterByRegex greater 10", () => {
  expect(getFilteredData("10:").length).toBe(4);
});

test("filterByRegex equals 10", () => {
  expect(getFilteredData("10").length).toBe(2);
});

test("filterByRegex between 10.3 and 10.7", () => {
  expect(getFilteredData("10.3:10.7").length).toBe(1);
});